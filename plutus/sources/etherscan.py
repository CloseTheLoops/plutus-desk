"""Etherscan v2: the token's complete transfer history, which makes every balance exact.

WHY THIS REPLACED PER-WALLET READS. Reading balances one wallet at a time from GMGN cost one call
per wallet and still missed everything a balance read cannot see between reads -- transfers
between our own wallets, staking, other venues -- leaving the position off for an hour or two
after each. Every one of those movements is a Transfer event. Holding the token's whole transfer
log makes every address's balance exact, including the pool, the staking contract and every
holder the vendor's ranked slices never showed, for a handful of calls a minute.

LIMITS (researched 2026-09-26 from docs.etherscan.io and etherscan.io/apis -- facts that rot;
re-verify monthly):
    Free   $0      3 calls/s   100,000 calls/day   Robinhood chain (4663) free until 2026-10-15
    Lite   $49/mo  5 calls/s   100,000 calls/day   REQUIRED for Robinhood chain from 2026-10-16
getLogs pages are capped (historically 1,000 records); the cap is no longer published, so
transfer_logs pages by BLOCK and deduplicates, which is correct for any cap.
The token-transfer endpoint (tokentx) omits logIndex, so two identical transfers in one
transaction are indistinguishable there. getLogs carries it; that is why getLogs is used.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

import requests

from plutus import config

log = config.get_logger("etherscan")

BASE = "https://api.etherscan.io/v2/api"
# Under the free plan's 3/s with headroom; the Lite plan allows 5/s.
RPS = float(os.environ.get("PLUTUS_ETHERSCAN_RPS") or 2.5)
DAILY_CAP = int(os.environ.get("PLUTUS_ETHERSCAN_DAILY") or 90_000)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PAGE = 1000
TIMEOUT = 30


class EtherscanError(RuntimeError):
    pass


class NoKey(EtherscanError):
    pass


class PlanRequired(EtherscanError):
    """The chain is not available on the key's plan -- e.g. Robinhood chain after 2026-10-15."""


class OverBudget(EtherscanError):
    pass


def api_key() -> str | None:
    """PLUTUS_ETHERSCAN_KEY, else ETHERSCAN_API_KEY, else data/etherscan.key (gitignored).

    PLUTUS_ETHERSCAN_DISABLE=1 forces "no key": tests set it so a key file sitting in data/
    can never turn a simulated run into real calls against the operator's quota.
    """
    if (os.environ.get("PLUTUS_ETHERSCAN_DISABLE") or "").strip() in ("1", "true", "yes"):
        return None
    for name in ("PLUTUS_ETHERSCAN_KEY", "ETHERSCAN_API_KEY"):
        if v := (os.environ.get(name) or "").strip():
            return v
    f = config.DATA_DIR / "etherscan.key"
    try:
        v = f.read_text(encoding="utf-8").strip().splitlines()[0].strip() if f.exists() else ""
    except (OSError, IndexError):
        v = ""
    return v or None


def available(chain: str) -> bool:
    return bool(api_key()) and config.chain(chain).etherscan_chain is not None


# ── pacing and the daily count, shared across processes ──────────────────────────────
_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()
_local_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    global _conn
    with _conn_lock:
        if _conn is None:
            c = sqlite3.connect(str(config.DB_PATH), timeout=10, check_same_thread=False,
                                isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=10000")
            c.execute("CREATE TABLE IF NOT EXISTS rate_state (k TEXT PRIMARY KEY, next_free REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS es_call_log (ts REAL NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_es_call_log_ts ON es_call_log(ts)")
            _conn = c
        return _conn


def _reserve() -> float:
    """Reserve the next slot (same design as the GMGN pacer): commit fast, sleep outside."""
    c = _db()
    c.execute("BEGIN IMMEDIATE")
    now = time.time()
    c.execute("DELETE FROM es_call_log WHERE ts < ?", (now - 86400,))
    day = c.execute("SELECT COUNT(*) FROM es_call_log WHERE ts > ?", (now - 86400,)).fetchone()[0]
    if day >= DAILY_CAP:
        c.execute("COMMIT")
        raise OverBudget(f"Etherscan daily budget spent ({day}/{DAILY_CAP})")
    row = c.execute("SELECT next_free FROM rate_state WHERE k='etherscan'").fetchone()
    nxt = float(row[0]) if row else 0.0
    if nxt > now + 120:
        nxt = now
    start = max(now + 0.004, nxt)
    c.execute("INSERT INTO rate_state (k, next_free) VALUES ('etherscan', ?) "
              "ON CONFLICT(k) DO UPDATE SET next_free=excluded.next_free", (start + 1.0 / RPS,))
    c.execute("INSERT INTO es_call_log (ts) VALUES (?)", (now,))
    c.execute("COMMIT")
    return max(0.0, start - time.time())


def _pause(seconds: float) -> None:
    c = _db()
    c.execute("INSERT INTO rate_state (k, next_free) VALUES ('etherscan', ?) "
              "ON CONFLICT(k) DO UPDATE SET next_free=MAX(next_free, excluded.next_free)",
              (time.time() + seconds,))


def budget() -> dict:
    try:
        day = _db().execute("SELECT COUNT(*) FROM es_call_log WHERE ts > ?",
                            (time.time() - 86400,)).fetchone()[0]
    except sqlite3.Error:
        return {"day": None, "day_cap": DAILY_CAP}
    return {"day": day, "day_cap": DAILY_CAP, "day_left": max(0, DAILY_CAP - day)}


# ── one request ─────────────────────────────────────────────────────────────────────
_NO_RECORDS = ("no records found", "no transactions found", "no logs found")


def _get(chain_id: int, params: dict, attempts: int = 4) -> Any:
    key = api_key()
    if not key:
        raise NoKey("no Etherscan API key: set PLUTUS_ETHERSCAN_KEY or put it in data/etherscan.key")
    q = {"chainid": chain_id, "apikey": key, **params}
    last = ""
    for attempt in range(1, attempts + 1):
        with _local_lock:
            wait = _reserve()
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(BASE, params=q, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last = f"network: {str(exc)[:100]}"
            time.sleep(0.5 * attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"http {r.status_code}"
            _pause(1.5 * attempt)
            continue
        try:
            j = r.json()
        except ValueError:
            last = f"bad json (http {r.status_code})"
            continue
        if "jsonrpc" in j:                              # proxy module: a JSON-RPC envelope
            if "error" in j:
                raise EtherscanError(f"rpc error: {str(j['error'])[:160]}")
            return j.get("result")
        status, msg = str(j.get("status")), str(j.get("message") or "")
        res = j.get("result")
        if status == "1":
            return res
        text = f"{msg} {res if isinstance(res, str) else ''}".lower()
        if any(s in text for s in _NO_RECORDS) or res == []:
            return []                                   # an empty answer, not a failure
        if "rate limit" in text or "max calls" in text:
            last = "rate limited"
            _pause(1.2 * attempt)
            continue
        if "invalid api key" in text or "missing" in text and "key" in text:
            raise NoKey(f"Etherscan rejected the key: {msg} {res}"[:200])
        if any(s in text for s in ("not supported", "upgrade", "api pro", "plan", "not available")):
            raise PlanRequired(
                f"Etherscan says this chain needs a higher plan ({msg}: {res}). Robinhood chain "
                f"requires the Lite plan ($49/mo) from 2026-10-16."[:300])
        raise EtherscanError(f"{msg}: {str(res)[:160]}")
    raise EtherscanError(f"Etherscan failed after {attempts} attempts ({last})")


# ── the calls Plutus makes ──────────────────────────────────────────────────────────
def latest_block(chain_id: int) -> int:
    return int(_get(chain_id, {"module": "proxy", "action": "eth_blockNumber"}), 16)


def token_supply(chain_id: int, token: str) -> int:
    """Total supply in raw units, exact."""
    return int(_get(chain_id, {"module": "stats", "action": "tokensupply",
                               "contractaddress": token}))


def decimals(chain_id: int, token: str) -> int:
    res = _get(chain_id, {"module": "proxy", "action": "eth_call", "to": token,
                          "data": "0x313ce567", "tag": "latest"})
    return int(res, 16)


def _parse(r: dict) -> dict | None:
    topics = r.get("topics") or []
    if len(topics) != 3 or (topics[0] or "").lower() != TRANSFER_TOPIC:
        return None                                     # not an ERC-20 Transfer
    data = r.get("data") or "0x0"
    return {"block": int(r["blockNumber"], 16), "log_index": int(r["logIndex"], 16),
            "tx_hash": r["transactionHash"].lower(),
            "from": "0x" + topics[1][-40:].lower(), "to": "0x" + topics[2][-40:].lower(),
            "raw": int(data, 16) if data not in ("0x", "") else 0,
            "ts": int(r.get("timeStamp") or "0x0", 16)}


def transfer_logs(chain_id: int, token: str, from_block: int, to_block: int) -> list[dict]:
    """Every Transfer of `token` in [from_block, to_block], oldest first, deduplicated.

    Pages by BLOCK: when a page comes back full, the next request starts at the last block seen
    (that block is re-read and deduplicated by (tx, logIndex)). Correct whatever the page cap is,
    and never skips logs past a cap the way page numbers can.
    """
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    cursor = from_block
    while cursor <= to_block:
        page = 1
        while True:
            rows = _get(chain_id, {"module": "logs", "action": "getLogs", "address": token,
                                   "topic0": TRANSFER_TOPIC, "fromBlock": cursor,
                                   "toBlock": to_block, "page": page, "offset": PAGE}) or []
            for r in rows:
                p = _parse(r)
                if p and (p["tx_hash"], p["log_index"]) not in seen:
                    seen.add((p["tx_hash"], p["log_index"]))
                    out.append(p)
            if len(rows) < PAGE:
                out.sort(key=lambda x: (x["block"], x["log_index"]))
                return out
            last = int(rows[-1]["blockNumber"], 16)
            if last > cursor:
                cursor = last                           # restart at the last block; dedup covers it
                break
            page += 1                                   # one block holds a full page: page within it
    out.sort(key=lambda x: (x["block"], x["log_index"]))
    return out
