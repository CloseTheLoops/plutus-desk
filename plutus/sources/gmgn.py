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
import sqlite3
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
COMMIT_BUDGET_S = 0.004        # headroom for committing the reservation before firing

# ── total call budget ─────────────────────────────────────────────────────────
# WHY PACING WAS NOT ENOUGH, AND WHY THIS IS THE PART THAT MATTERS. MIN_INTERVAL_S caps calls
# per SECOND. It says nothing about calls per hour, and a vendor's abuse detection watches
# sustained volume, not just instantaneous burst. A thousand calls spread evenly over ninety
# minutes never trips the pacer once and is exactly the pattern that gets an API key suspended.
#
# So there is a hard ceiling, shared across processes in the same database as the pacer. When it
# is reached, calls do not queue or slow down -- they REFUSE, loudly, naming what to do. A tool
# that can spend an account's entire quota by being used normally is not finished.
MAX_CALLS_HOUR = int(os.environ.get("PLUTUS_MAX_CALLS_HOUR") or 900)
MAX_CALLS_DAY = int(os.environ.get("PLUTUS_MAX_CALLS_DAY") or 6000)

# ── background work gets its own, smaller ceiling ─────────────────────────────
# WHY. With nobody using the desk, the background loop spent 9,999 of a 10,000 daily budget and
# held it there from 01:00 onward. One ceiling for everything meant background sweeps could
# consume the whole of it, and then the operator's own actions -- a full pull, onboarding a token,
# a live campaign -- were refused by work nobody had asked for. Background calls now stop at a
# share of each cap; the rest is kept for what the operator does, which is never starved.
BG_HOUR_SHARE = float(os.environ.get("PLUTUS_BG_HOUR_SHARE") or 0.5)
BG_DAY_SHARE = float(os.environ.get("PLUTUS_BG_DAY_SHARE") or 0.6)


def _caps(background: bool) -> tuple[int, int]:
    if background:
        return int(MAX_CALLS_HOUR * BG_HOUR_SHARE), int(MAX_CALLS_DAY * BG_DAY_SHARE)
    return MAX_CALLS_HOUR, MAX_CALLS_DAY

# ── response cache ────────────────────────────────────────────────────────────
# WHY DELETE + RE-ONBOARD SHOULD BE CHEAP, WITHOUT WEAKENING WHAT DELETE MEANS.
# Deleting a token removes the token: its rows, its id, its history. Re-adding it is a genuine
# first-time onboard. None of that requires the tool to FORGET WHAT THE VENDOR SAID SIXTY
# SECONDS AGO -- that is not the token's data, it is an answer to a question, and asking the
# same question again that soon gets the same answer at the cost of the account.
#
# So the cache is keyed by the CALL, which contains the token address, and lives outside
# everything token_id-keyed. A delete does not touch it. Re-onboarding inside the TTL rebuilds
# a fresh token from answers already paid for, and costs nothing.
#
# TTLs are per route and short. Anything the operator explicitly asks to refresh passes
# fresh=True and bypasses the cache entirely -- an explicit pull must never be served a copy.
CACHE_TTL = {
    ("token", "info"): 300,
    ("token", "pool"): 120,
    ("token", "security"): 900,
    ("token", "traders"): 180,
    ("portfolio", "token-balance"): 120,
    ("portfolio", "info"): 120,
    ("gas-price",): 60,
    # ("order", "quote") is deliberately absent: a price is the one thing never served stale.
}
CACHE_ON = os.environ.get("PLUTUS_NO_CACHE", "").strip() not in ("1", "true", "yes")


def _cache_ttl(args: tuple[str, ...]) -> int:
    return CACHE_TTL.get(tuple(args[:2]), CACHE_TTL.get(tuple(args[:1]), 0))


def _cache_key(args: tuple[str, ...]) -> str:
    return json.dumps(args, separators=(",", ":"))


def _cache_get(args: tuple[str, ...]) -> tuple[bool, Any]:
    ttl = _cache_ttl(args)
    if not (CACHE_ON and ttl):
        return False, None
    try:
        row = _pace_db().execute(
            "SELECT body FROM api_cache WHERE key=? AND ts > ?",
            (_cache_key(args), time.time() - ttl)).fetchone()
    except sqlite3.Error:
        return False, None
    if row is None:
        return False, None
    try:
        return True, json.loads(row[0])
    except ValueError:
        return False, None


def _cache_put(args: tuple[str, ...], body: Any) -> None:
    if not (CACHE_ON and _cache_ttl(args)):
        return
    try:
        c = _pace_db()
        c.execute("INSERT INTO api_cache (key, ts, body) VALUES (?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts, body=excluded.body",
                  (_cache_key(args), time.time(), json.dumps(body)))
        c.execute("DELETE FROM api_cache WHERE ts < ?", (time.time() - 3600,))
    except (sqlite3.Error, TypeError, ValueError):
        pass                               # a cache that cannot write is still correct

ANALYTICS_HOME = os.environ.get("PLUTUS_GMGN_HOME") or os.path.expanduser("~/.gmgn-analytics")

# Commands that would require a private key. Calling one is a programming error in this layer.
_FORBIDDEN = {("swap",), ("multi-swap",), ("order", "strategy")}

_last_call = 0.0
_rate_lock = threading.Lock()


class GmgnError(RuntimeError):
    pass


class BudgetExceeded(GmgnError):
    """The call budget is spent. Not a failure to retry -- a limit to wait out."""


class RateLimited(GmgnError):
    """GMGN answered 429. Every caller has already been paused; never retried on another path."""


class Transient(GmgnError):
    """A 5xx or a network failure: retry the same path, never re-send it through the CLI.

    The CLI calls the same API. A 500 there is a 500 here, so falling back to it spent a second
    call on the same failure and logged two warnings for it.
    """


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


def _http_call(args: tuple[str, ...], background: bool = False) -> Any:
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

    _pace(background)
    try:
        r = _get_session().request(
            method, HOST + path, params=params, timeout=TIMEOUT,
            headers={"X-APIKEY": key, "Content-Type": "application/json",
                     "User-Agent": "gmgn-cli/1.5.6"})
    except requests.RequestException as exc:
        # A dropped connection or a timeout used to escape as a non-GMGN exception, which no
        # caller catches -- one timeout aborted an entire wallet sweep.
        raise Transient(f"network {path}: {str(exc)[:120]}") from None
    if r.status_code == 429:
        pause_all(min((_cooldown("RATE_LIMIT " + r.text) or 35) + 3, MAX_WAIT_S), path)
        raise RateLimited(f"RATE_LIMIT {path}: {r.text[:160]}")
    if r.status_code >= 500:
        raise Transient(f"http {r.status_code} {path}")
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


_pace_conn: sqlite3.Connection | None = None
_pace_conn_lock = threading.Lock()


def _pace_db() -> sqlite3.Connection:
    """A connection used ONLY by the pacer.

    Separate from db.connect() on purpose: that connection is often mid-transaction writing
    balances or trades, and issuing BEGIN IMMEDIATE on a connection already in a transaction is
    an error. The pacer must never be able to disturb, or be disturbed by, real work.
    """
    global _pace_conn
    with _pace_conn_lock:
        if _pace_conn is None:
            c = sqlite3.connect(str(config.DB_PATH), timeout=10, check_same_thread=False,
                                isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=10000")
            c.execute("CREATE TABLE IF NOT EXISTS rate_state ("
                      "k TEXT PRIMARY KEY, next_free REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS call_log (ts REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS api_cache ("
                      "key TEXT PRIMARY KEY, ts REAL NOT NULL, body TEXT NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_call_log_ts ON call_log(ts)")
            _pace_conn = c
        return _pace_conn


def _reserve_slot(background: bool = False) -> float:
    """Claim the next free moment to call, and return how long to wait for it.

    RESERVATION, NOT A HELD LOCK. The obvious implementation -- open a write transaction, read
    the clock, sleep, write it back, commit -- holds SQLite's single writer lock for the whole
    sleep, so every unrelated write (balances, trades, pool observations) queues behind the rate
    limiter. Instead this moves `next_free` forward by one interval and commits in microseconds;
    the caller then sleeps outside the transaction until the slot it was given. Concurrent
    callers get distinct, consecutive slots, which is exactly the intended behaviour.

    The row is keyed by nothing token-specific, because the limit belongs to the API KEY.
    """
    global _last_call
    now = time.time()
    try:
        c = _pace_db()
        c.execute("BEGIN IMMEDIATE")
        # Read the clock AFTER the write lock is held, not before. Waiting for the lock can
        # take several milliseconds, and anchoring the reservation to a pre-lock timestamp
        # hands out a slot that is already in the past -- so the call fires late while the NEXT
        # slot was recorded from the stale reading, leaving the two closer than the interval.
        now = time.time()
        row = c.execute("SELECT next_free FROM rate_state WHERE k='gmgn'").fetchone()
        nxt = float(row[0]) if row else 0.0
        # A value far in the future means a clock change or a crashed reservation, not a real
        # queue. Waiting it out would stall every caller for as long as the skew. The bound is
        # the longest DELIBERATE wait -- a global pause after a 429 -- so a real pause is kept.
        if nxt > now + MAX_WAIT_S + 60:
            nxt = now
        # COMMIT_BUDGET keeps the slot marginally in the future so that committing and
        # returning does not overshoot it. Without it the very first call of a process -- the
        # one that finds the clock idle and so waits zero -- fires a few milliseconds after the
        # slot it recorded, and the call after it lands short of a full interval.
        # Budget check inside the same transaction that reserves the slot: one write lock, and
        # two processes cannot both be told they have the last call left.
        c.execute("DELETE FROM call_log WHERE ts < ?", (now - 86400,))
        hour = c.execute("SELECT COUNT(*) FROM call_log WHERE ts > ?",
                         (now - 3600,)).fetchone()[0]
        day = c.execute("SELECT COUNT(*) FROM call_log WHERE ts > ?",
                        (now - 86400,)).fetchone()[0]
        hcap, dcap = _caps(background)
        if hour >= hcap or day >= dcap:
            c.execute("COMMIT")
            which = "hourly" if hour >= hcap else "daily"
            raise BudgetExceeded(
                f"{'background ' if background else ''}{which} GMGN budget spent: {hour} this "
                f"hour / {day} today (cap {hcap if which == 'hourly' else dcap})")
        c.execute("INSERT INTO call_log (ts) VALUES (?)", (now,))
        start = max(now + COMMIT_BUDGET_S, nxt)
        c.execute("INSERT INTO rate_state (k, next_free) VALUES ('gmgn', ?) "
                  "ON CONFLICT(k) DO UPDATE SET next_free=excluded.next_free",
                  (start + MIN_INTERVAL_S,))
        c.execute("COMMIT")
        _last_call = start           # keep the fallback clock usable if the db later fails
        # Measure the wait against the time it is NOW, not the `now` read before the
        # transaction. Acquiring the write lock takes a variable few milliseconds, and sleeping
        # a delta computed before it means firing that much LATE. A call that fires late
        # followed by one that fires on time leaves a gap shorter than the interval -- which is
        # precisely the violation this whole mechanism exists to prevent. Sleeping to the
        # absolute slot instead absorbs the overhead.
        return max(0.0, start - time.time())
    except sqlite3.Error as exc:
        log.debug("pacer fell back to the in-process clock: %s", exc)
        start = max(now, _last_call + MIN_INTERVAL_S)
        _last_call = start
        return max(0.0, start - now)


def _pace(background: bool = False) -> None:
    """Hold the global minimum interval between calls -- across threads AND processes.

    WHY THIS IS NOT JUST A LOCK. A threading.Lock serialises the calls inside one interpreter and
    knows nothing about any other process using the same API key. The service tracks tokens
    continuously while an operator can run `plutus.cli tick` beside it; each kept its own private
    clock, so the key saw two independent streams and twice the intended rate. The interval
    belongs to the key, so its state has to live somewhere both processes can see -- and the
    project already has exactly one such place.

    The in-process lock is kept in front of it: it costs nothing, orders this process's own
    threads, and keeps them from contending on the database row one at a time.
    """
    with _rate_lock:
        wait = _reserve_slot(background)  # raises BudgetExceeded rather than spending
    if wait > 0:
        time.sleep(wait)


def budget(background: bool = False, need: int = 1) -> dict:
    """Where the budget stands for a tier, and WHEN `need` more calls will be allowed.

    `left` is the smaller of what the hour and the day allow, because either can be the one that
    binds -- the sweep that spent a daily budget to the last call only ever looked at the hour.
    `resumes_at` is when enough calls will have aged out of their window: a caller that cannot
    afford its work sleeps until then instead of retrying against a spent cap.
    """
    now = time.time()
    hcap, dcap = _caps(background)
    try:
        c = _pace_db()
        hour = c.execute("SELECT COUNT(*) FROM call_log WHERE ts > ?",
                         (now - 3600,)).fetchone()[0]
        day = c.execute("SELECT COUNT(*) FROM call_log WHERE ts > ?",
                        (now - 86400,)).fetchone()[0]
        resumes = now
        for window, used, cap in ((3600, hour, hcap), (86400, day, dcap)):
            excess = used - (cap - need)
            if excess > 0:
                row = c.execute("SELECT ts FROM call_log WHERE ts > ? ORDER BY ts LIMIT 1 "
                                "OFFSET ?", (now - window, excess - 1)).fetchone()
                if row:
                    resumes = max(resumes, float(row[0]) + window)
    except sqlite3.Error:
        return {"hour": None, "day": None, "hour_cap": hcap, "day_cap": dcap, "left": None,
                "hour_left": None, "day_left": None, "resumes_at": now, "ok": True}
    hl, dl = max(0, hcap - hour), max(0, dcap - day)
    return {"hour": hour, "day": day, "hour_cap": hcap, "day_cap": dcap,
            "hour_left": hl, "day_left": dl, "left": min(hl, dl), "resumes_at": resumes,
            "background": background, "ok": hour < hcap and day < dcap}


_PAUSES: list[float] = []


def pause_all(seconds: float, why: str = "") -> None:
    """Hold EVERY caller -- every thread, every process on this key -- for `seconds`.

    A 429 is about the key, not the thread that happened to receive it. Handled per thread, the
    other workers kept firing into the same limit and extending it, and the CLI fallback made the
    same request again on a second transport. So the pause is written into the shared rate_state
    row: every reservation after it is handed a slot after the pause ends.
    """
    global _last_call
    until = time.time() + max(0.0, seconds)
    extended = False
    try:
        c = _pace_db()
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT next_free FROM rate_state WHERE k='gmgn'").fetchone()
        if until > (float(row[0]) if row else 0.0):
            c.execute("INSERT INTO rate_state (k, next_free) VALUES ('gmgn', ?) "
                      "ON CONFLICT(k) DO UPDATE SET next_free=excluded.next_free", (until,))
            extended = True
        c.execute("COMMIT")
    except sqlite3.Error:
        _last_call, extended = max(_last_call, until), True
    if extended:
        _PAUSES.append(time.time())
        recent = [p for p in _PAUSES if time.time() - p < 600]
        _PAUSES[:] = recent
        # One line when a burst of 429s starts, then silence for ten minutes: during a storm the
        # old code logged every pause.
        if len(recent) == 1:
            log.warning("GMGN rate limit%s — every worker paused for %ds",
                        f" on {why}" if why else "", int(seconds))
        elif len(recent) in (10, 50):
            log.warning("GMGN rate limits continuing: %d pauses in the last 10 minutes",
                        len(recent))


def _cooldown(err: str) -> int | None:
    if "RATE_LIMIT" not in err and "429" not in err:
        return None
    m = re.search(r"~(\d+)s remaining", err)
    return int(m.group(1)) if m else 35


def call(*args: str, attempts: int = 3, fresh: bool = False, background: bool = False) -> Any:
    """Run one CLI command with --raw and return parsed JSON.

    attempts=1 means fail fast: for callers inside a latency-sensitive loop where waiting out a
    cooldown would starve everything queued behind it.
    """
    global _last_call
    for n in range(len(args), 0, -1):
        if tuple(args[:n]) in _FORBIDDEN:
            raise GmgnError(f"{' '.join(args[:n])!r} requires a private key — "
                            "the analysis layer must never call it")

    # Cache first: a hit costs no call, no budget and no wait.
    if not fresh:
        hit, body = _cache_get(args)
        if hit:
            log.debug("cache hit: %s", " ".join(args[:3]))
            return body

    if USE_HTTP:
        for attempt in range(1, attempts + 1):
            try:
                got = _http_call(args, background=background)
                _cache_put(args, got)
                return got
            except _NoRoute:
                break                              # no route — fall through to the CLI
            except BudgetExceeded:
                raise                      # a spent budget is not something to retry elsewhere
            except RateLimited:
                # The pause is already recorded for every caller, so the retry waits it out in
                # _pace. What must NOT happen is the CLI making the same request on the same key
                # mid-limit: that was a second call into an active 429.
                if attempt == attempts:
                    raise
            except Transient as exc:
                if attempt == attempts:
                    raise
                log.debug("transient failure on %s (attempt %d): %s", " ".join(args[:2]),
                          attempt, exc)
                time.sleep(0.5 * attempt)
            except GmgnError:
                log.warning("http path failed for %s, falling back to the CLI",
                            " ".join(args[:3]))
                break

    cmd = [*CLI, *args, "--raw"]

    for attempt in range(1, attempts + 1):
        _pace(background)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT,
                               encoding="utf-8", env=_env(), creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            log.warning("gmgn timeout (attempt %d): %s", attempt, " ".join(args[:3]))
            continue
        if p.returncode == 0 and p.stdout.strip():
            try:
                got = json.loads(p.stdout)
                _cache_put(args, got)
                return got
            except json.JSONDecodeError as exc:
                raise GmgnError(f"bad JSON from {' '.join(args[:3])}: {exc}") from None
        err = (p.stderr or p.stdout or "")[:400]
        if attempt == attempts:
            break
        if (wait := _cooldown(err)) is not None:
            # Pause EVERY caller, not this thread; the next _pace waits it out.
            pause_all(min(wait + 3, MAX_WAIT_S), " ".join(args[:2]))
        else:
            log.warning("gmgn failed (attempt %d): %s", attempt, err[:200])
            time.sleep(2 * attempt)
    raise GmgnError(f"gmgn-cli {' '.join(args[:4])} failed after {attempts} attempts")


# ── typed wrappers ────────────────────────────────────────────────────────────
def token_info(chain: str, address: str, background: bool = False) -> dict:
    """NOTE the data shape: `price` comes back as a NESTED OBJECT, not a scalar, and several
    numerics are strings. Flattened once here so no caller has to know that."""
    d = call("token", "info", "--chain", chain, "--address", address, background=background)
    return d if isinstance(d, dict) else {}


def token_pool(chain: str, address: str, fresh: bool = False, background: bool = False) -> dict:
    return call("token", "pool", "--chain", chain, "--address", address, fresh=fresh,
                background=background) or {}


def balance_cached(chain: str, wallet: str, token: str) -> bool:
    """Would token_balance for this wallet be served from cache right now -- i.e. cost nothing?

    Lets a sweep plan against the budget: a cached read is free, so it never needs allowance.
    The key must match token_balance's call exactly, or everything looks uncached.
    """
    hit, _ = _cache_get(("portfolio", "token-balance", "--chain", chain, "--wallet", wallet,
                         "--token", token))
    return hit


def token_security(chain: str, address: str) -> dict:
    return call("token", "security", "--chain", chain, "--address", address) or {}


def traders(chain: str, address: str, order_by: str = "amount_percentage",
            tag: str | None = None, limit: int = 100, attempts: int = 2,
            background: bool = False) -> list[dict]:
    args = ["token", "traders", "--chain", chain, "--address", address,
            "--limit", str(limit), "--order-by", order_by]
    if tag:
        args += ["--tag", tag]
    d = call(*args, attempts=attempts, background=background)
    return (d or {}).get("list") or []


def token_balance(chain: str, wallet: str, token: str, fresh: bool = False,
                  background: bool = False) -> tuple[float, int | None]:
    """Direct balance for one wallet. Returns (tokens, block_height_of_last_change).

    `height` is free provenance the vendor hands us: the block at which this balance last CHANGED.
    Recorded, never discarded.
    """
    d = call("portfolio", "token-balance", "--chain", chain, "--wallet", wallet,
             "--token", token, attempts=2, fresh=fresh, background=background)
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
