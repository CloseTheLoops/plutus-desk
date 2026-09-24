"""GMGN access via the official `gmgn-cli` subprocess.

We shell out rather than re-implement Ed25519 request signing: auth stays vendor-maintained and
cannot rot silently on our side.

THREE BEHAVIOURS THAT ARE NOT OPTIONAL, each learned from a real failure:
1. **Honour the 429 reset.** Retrying inside a cooldown EXTENDS the ban. Parse the advertised
   remaining seconds, add a margin, never hammer.
2. **CREATE_NO_WINDOW on Windows.** npm installs the CLI as a `.cmd` shim that only cmd.exe can
   exec, and Windows gives every new console process a VISIBLE window — so without this flag the
   desk flashes a console on every single API call. Unnoticeable at one call, maddening at 155.
3. **Never the trading key's bucket for analysis.** The CLI selects a key by its home directory,
   so an analytics workload points HOME at a directory holding the analytics key.

THIS MODULE IS READ-ONLY BY CONSTRUCTION. `swap`, `multi-swap` and `order strategy` are the only
commands that need a private key and none of them appear here. The analysis layer cannot trade.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from typing import Any

import requests

from plutus import config

log = config.get_logger("gmgn")

CLI = ["cmd.exe", "/c", "gmgn-cli"] if os.name == "nt" else ["gmgn-cli"]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
TIMEOUT = 45
MAX_WAIT_S = 120
MIN_INTERVAL_S = 0.06          # self-cap well inside the paid bucket
ANALYTICS_HOME = os.environ.get("PLUTUS_GMGN_HOME") or os.path.expanduser("~/.gmgn-analytics")

# Commands that would require a private key. Calling one is a programming error in this layer.
_FORBIDDEN = {("swap",), ("multi-swap",), ("order", "strategy")}

_last_call = 0.0
_rate_lock = threading.Lock()


class GmgnError(RuntimeError):
    pass


def _env() -> dict[str, str] | None:
    """Point gmgn-cli at the analytics profile, or inherit if there isn't one.

    WHAT THE CREDENTIAL ACTUALLY IS. Both halves live in the profile's `.env`: `GMGN_API_KEY`
    identifies, `GMGN_PRIVATE_KEY` signs. `keypair.pem` is a leftover artifact of `gmgn-cli
    config` generating the pair -- the CLI does not read it, and a profile without one is
    perfectly healthy. (Verified: a profile holding only `.env` authenticates signed endpoints.)

    Half a credential is still worse than none, because none falls back to a working default
    while a partial one silently fails every call -- balances then read 0.0 and it looks like a
    data problem. So this checks for the two fields that matter, and nothing else.
    """
    cfg = os.path.join(ANALYTICS_HOME, ".config", "gmgn")
    key_file = os.path.join(cfg, ".env")
    if not os.path.isfile(key_file):
        return None                      # inherit; the default key is whatever the CLI finds
    try:
        body = open(key_file, encoding="utf-8").read()
    except OSError as exc:
        raise GmgnError(f"cannot read {key_file}: {exc}") from None
    missing = [f for f in ("GMGN_API_KEY", "GMGN_PRIVATE_KEY") if f"{f}=" not in body]
    if missing:
        raise GmgnError(
            f"{key_file} is missing {' and '.join(missing)}, so calls cannot be "
            f"{'identified' if 'GMGN_API_KEY' in missing else 'signed'} and every balance would "
            f"silently read zero. Re-apply the key for THIS profile "
            f"({'HOME' if os.name != 'nt' else 'USERPROFILE'}={ANALYTICS_HOME} "
            f"gmgn-cli config --apply <api_key>), or point PLUTUS_GMGN_HOME at a complete "
            f"profile.")
    e = os.environ.copy()
    e["USERPROFILE" if os.name == "nt" else "HOME"] = ANALYTICS_HOME
    return e


# ── direct HTTP transport ─────────────────────────────────────────────────────
# WHY THIS EXISTS. Every read below is "exist auth" in the vendor's own client: an API key in a
# header plus a timestamp and a client_id in the query. No signature, no private key. Going
# straight to the endpoint removes a Node process spawn per call -- measured at 0.57s via the
# CLI against 0.30s over HTTP on an idle machine, and far worse than that on a loaded one, where
# CLI startup was clocked at 2-3s while bare node stayed at 0.4s.
#
# IT ALSO TIGHTENS THE TRUST BOUNDARY RATHER THAN LOOSENING IT. This path never reads
# GMGN_PRIVATE_KEY, so it is structurally incapable of signing a swap -- the analysis layer's
# rule stops being a blocklist it must remember to check and becomes a capability it does not
# have. Anything requiring a signature has no route here and falls through to the CLI, where
# _FORBIDDEN still refuses it.
#
# Every route is parity-tested against the CLI in tests/test_http_parity.py. A faster transport
# that returns a different shape is worse than a slow one, because the difference shows up as
# wrong numbers rather than as an error.
HOST = "https://openapi.gmgn.ai"
USE_HTTP = os.environ.get("PLUTUS_NO_HTTP", "").strip() not in ("1", "true", "yes")

# (command, subcommand) -> (method, path, {cli_flag: query_param})
_ROUTES: dict[tuple, tuple] = {
    ("token", "info"): ("GET", "/v1/token/info", {"--chain": "chain", "--address": "address"}),
    ("token", "pool"): ("GET", "/v1/token/pool_info", {"--chain": "chain", "--address": "address"}),
    ("token", "security"): ("GET", "/v1/token/security",
                            {"--chain": "chain", "--address": "address"}),
    ("token", "traders"): ("GET", "/v1/market/token_top_traders",
                           {"--chain": "chain", "--address": "address", "--limit": "limit",
                            "--order-by": "order_by", "--tag": "tag"}),
    ("portfolio", "token-balance"): ("GET", "/v1/user/wallet_token_balance",
                                     {"--chain": "chain", "--wallet": "wallet_address",
                                      "--token": "token_address"}),
    ("portfolio", "info"): ("GET", "/v1/user/info", {}),
    ("order", "quote"): ("GET", "/v1/trade/quote",
                         {"--chain": "chain", "--from": "from_address",
                          "--input-token": "input_token", "--output-token": "output_token",
                          "--amount": "input_amount", "--slippage": "slippage"}),
    ("gas-price",): ("GET", "/v1/trade/gas_price", {"--chain": "chain"}),
}

_session = None
_session_lock = threading.Lock()


def _api_key() -> str | None:
    """The API key for the profile in use. Deliberately does NOT read GMGN_PRIVATE_KEY."""
    path = os.path.join(ANALYTICS_HOME, ".config", "gmgn", ".env")
    if not os.path.isfile(path):
        path = os.path.join(os.path.expanduser("~"), ".config", "gmgn", ".env")
    try:
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if ln.startswith("GMGN_API_KEY="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return os.environ.get("GMGN_API_KEY") or None


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            # One pooled connection per worker, so TLS is negotiated once rather than per call.
            ad = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=0)
            s.mount("https://", ad)
            _session = s
        return _session


def _http_call(args: tuple[str, ...]) -> Any:
    """Serve one call over HTTP, or raise _NoRoute so the caller falls back to the CLI."""
    route = _ROUTES.get(tuple(args[:2])) or _ROUTES.get(tuple(args[:1]))
    if route is None:
        raise _NoRoute
    key = _api_key()
    if not key:
        raise _NoRoute
    method, path, flagmap = route
    rest = [a for a in args if a != "--raw"]
    rest = rest[2:] if tuple(args[:2]) in _ROUTES else rest[1:]
    params: dict[str, Any] = {}
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag not in flagmap:                 # an argument this route does not model
            raise _NoRoute
        params[flagmap[flag]] = rest[i + 1]
        i += 2
    params["timestamp"] = int(time.time())
    params["client_id"] = str(uuid.uuid4())

    _pace()
    r = _get_session().request(
        method, HOST + path, params=params, timeout=TIMEOUT,
        headers={"X-APIKEY": key, "Content-Type": "application/json",
                 "User-Agent": "gmgn-cli/1.5.6"})
    if r.status_code == 429:
        raise GmgnError(f"RATE_LIMIT {path}: {r.text[:160]}")
    if r.status_code != 200:
        raise GmgnError(f"http {r.status_code} {path}: {r.text[:160]}")
    body = r.json()
    # The CLI hands callers the payload, not the envelope. Match it exactly.
    if isinstance(body, dict) and "code" in body and "data" in body:
        if body.get("code") not in (0, None):
            raise GmgnError(f"api code {body.get('code')} {path}: {str(body)[:160]}")
        return body["data"]
    return body


class _NoRoute(Exception):
    """This call has no HTTP route; use the CLI."""


def _pace() -> None:
    """Hold the global minimum interval between calls, across threads.

    This has to be a real lock now that the balance sweep calls concurrently. Without it every
    worker reads the same `_last_call`, computes the same gap, sleeps it, and fires together --
    which is precisely the burst the interval exists to prevent. The lock makes the interval a
    property of the process rather than of each thread.
    """
    global _last_call
    with _rate_lock:
        gap = MIN_INTERVAL_S - (time.time() - _last_call)
        if gap > 0:
            time.sleep(gap)
        _last_call = time.time()


def _cooldown(err: str) -> int | None:
    if "RATE_LIMIT" not in err and "429" not in err:
        return None
    m = re.search(r"~(\d+)s remaining", err)
    return int(m.group(1)) if m else 35


def call(*args: str, attempts: int = 3) -> Any:
    """Run one CLI command with --raw and return parsed JSON.

    attempts=1 means fail fast: for callers inside a latency-sensitive loop where waiting out a
    cooldown would starve everything queued behind it.
    """
    global _last_call
    for n in range(len(args), 0, -1):
        if tuple(args[:n]) in _FORBIDDEN:
            raise GmgnError(f"{' '.join(args[:n])!r} requires a private key — "
                            "the analysis layer must never call it")

    if USE_HTTP:
        try:
            return _http_call(args)
        except _NoRoute:
            pass                                   # no route — fall through to the CLI
        except GmgnError:
            log.warning("http path failed for %s, falling back to the CLI", " ".join(args[:3]))

    cmd = [*CLI, *args, "--raw"]
    for attempt in range(1, attempts + 1):
        _pace()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT,
                               encoding="utf-8", env=_env(), creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            log.warning("gmgn timeout (attempt %d): %s", attempt, " ".join(args[:3]))
            continue
        if p.returncode == 0 and p.stdout.strip():
            try:
                return json.loads(p.stdout)
            except json.JSONDecodeError as exc:
                raise GmgnError(f"bad JSON from {' '.join(args[:3])}: {exc}") from None
        err = (p.stderr or p.stdout or "")[:400]
        if attempt == attempts:
            break
        if (wait := _cooldown(err)) is not None:
            wait = min(wait + 3, MAX_WAIT_S)
            log.warning("gmgn rate-limited, honouring reset in %ds", wait)
            time.sleep(wait)
        else:
            log.warning("gmgn failed (attempt %d): %s", attempt, err[:200])
            time.sleep(2 * attempt)
    raise GmgnError(f"gmgn-cli {' '.join(args[:4])} failed after {attempts} attempts")


# ── typed wrappers ────────────────────────────────────────────────────────────
def token_info(chain: str, address: str) -> dict:
    """NOTE the data shape: `price` comes back as a NESTED OBJECT, not a scalar, and several
    numerics are strings. Flattened once here so no caller has to know that."""
    d = call("token", "info", "--chain", chain, "--address", address)
    return d if isinstance(d, dict) else {}


def token_pool(chain: str, address: str) -> dict:
    return call("token", "pool", "--chain", chain, "--address", address) or {}


def token_security(chain: str, address: str) -> dict:
    return call("token", "security", "--chain", chain, "--address", address) or {}


def traders(chain: str, address: str, order_by: str = "amount_percentage",
            tag: str | None = None, limit: int = 100, attempts: int = 2) -> list[dict]:
    args = ["token", "traders", "--chain", chain, "--address", address,
            "--limit", str(limit), "--order-by", order_by]
    if tag:
        args += ["--tag", tag]
    d = call(*args, attempts=attempts)
    return (d or {}).get("list") or []


def token_balance(chain: str, wallet: str, token: str) -> tuple[float, int | None]:
    """Direct balance for one wallet. Returns (tokens, block_height_of_last_change).

    `height` is free provenance the vendor hands us: the block at which this balance last CHANGED.
    Recorded, never discarded.
    """
    d = call("portfolio", "token-balance", "--chain", chain, "--wallet", wallet,
             "--token", token, attempts=2)
    for e in (d or {}).get("balances") or []:
        if (e.get("token_address") or "").lower() == token.lower():
            return float(e.get("balance") or 0), (int(e["height"]) if e.get("height") else None)
    return 0.0, None


def bound_wallets() -> list[str]:
    """Wallets bound to the API key. NOTE: `portfolio info` takes no --chain argument."""
    d = call("portfolio", "info", attempts=2) or {}
    out: list[str] = []
    for key in ("wallets", "list", "data"):
        for w in (d.get(key) or []):
            a = w.get("address") if isinstance(w, dict) else w
            if a:
                out.append(str(a))
    return out


def quote(chain: str, frm: str, input_token: str, output_token: str,
          amount: int, slippage: int = 5) -> dict:
    """Read-only swap quote. API key only — no private key, nothing submitted.

    DATA SHAPE: everything useful is nested under `tx`, not at the top level. Returned flattened.
    """
    d = call("order", "quote", "--chain", chain, "--from", frm,
             "--input-token", input_token, "--output-token", output_token,
             "--amount", str(int(amount)), "--slippage", str(slippage), attempts=2) or {}
    tx = d.get("tx") or {}
    return {**tx, "output_amount": d.get("output_amount"), "input_amount": d.get("input_amount")}
