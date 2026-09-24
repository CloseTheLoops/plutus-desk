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
import time
from typing import Any

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


class GmgnError(RuntimeError):
    pass


def _env() -> dict[str, str] | None:
    """Point gmgn-cli at the analytics profile, or inherit if there isn't one.

    THE BROKEN STATE THIS REFUSES TO RUN IN. gmgn-cli signs every request with
    `keypair.pem`; the `.env` only identifies which API key is signing. A profile holding one
    without the other authenticates as nobody. Every call then fails, `trackers` logs a warning
    per wallet, the balance simply never gets recorded, and `ledger` defaults what it cannot find
    to 0.0 -- so the desk reports wallets holding nothing and looks like a data problem.

    Half a profile is strictly worse than no profile, because no profile falls back to a working
    default. So this raises instead of returning a config that cannot sign.
    """
    cfg = os.path.join(ANALYTICS_HOME, ".config", "gmgn")
    key_file, pem = os.path.join(cfg, ".env"), os.path.join(cfg, "keypair.pem")
    if not os.path.isfile(key_file):
        return None                      # inherit; the default key is whatever the CLI finds
    if not os.path.isfile(pem):
        raise GmgnError(
            f"{ANALYTICS_HOME} has an API key but no keypair.pem, so nothing can be signed and "
            f"every balance would silently read zero. Generate one FOR THIS PROFILE'S OWN KEY "
            f"({'HOME' if os.name != 'nt' else 'USERPROFILE'}={ANALYTICS_HOME} gmgn-cli config) "
            f"-- do not copy another profile's keypair, it belongs to a different API key. "
            f"Or set PLUTUS_GMGN_HOME to a complete profile.")
    e = os.environ.copy()
    e["USERPROFILE" if os.name == "nt" else "HOME"] = ANALYTICS_HOME
    return e


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

    cmd = [*CLI, *args, "--raw"]
    for attempt in range(1, attempts + 1):
        gap = MIN_INTERVAL_S - (time.time() - _last_call)
        if gap > 0:
            time.sleep(gap)
        _last_call = time.time()
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
