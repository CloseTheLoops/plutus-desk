"""The chain's own JSON-RPC: the token's transfer history for free, no key.

WHY. The transfer ledger needs every Transfer event of the token and its total supply. Any
Ethereum-style node serves both (eth_getLogs, eth_call totalSupply) -- Etherscan is one paid
way to get them, the chain's public RPC is a free one. Measured on Robinhood chain 2026-09-26:
FAITH's full history (27,067 transfers from the mint) rebuilt from the public RPC summed to
total supply to the last unit.

WHAT THE PUBLIC ENDPOINT DOES (measured 2026-09-26, rpc.mainnet.chain.robinhood.com -- facts
that rot; re-verify monthly):
  * eth_getLogs returns at most 10,000 logs per query ("exceeds limit of 10000") and times out
    on very wide busy ranges ("log query timed out"). Both mean: ask for fewer blocks.
  * Blocks past the node's head come back as an EMPTY LIST, not an error. Asking a node that is
    behind would silently drop transfers, so every range is capped at the head of the SAME
    endpoint that serves it, less a confirmation margin.
  * State is kept for ~1,000 blocks only ("historical state ... is not available"), so total
    supply is read at the block the ledger synced to, while that block is still recent.
  * No published rate limit; the docs call it "rate-limited, not for production". The ledger
    needs ~4 calls a minute; the pacer holds every process on this machine to PLUTUS_RPC_RPS.
Rejected: dRPC's keyless tier (refused 1,000-block ranges, no state at a block), and Blockscout
(Cloudflare challenge on every scripted request).

Extra endpoints (e.g. a free Alchemy URL) can be added with PLUTUS_RPC_<CHAIN>=url1,url2; the
first that answers is used, and a fallback is only used for ranges it has itself reached.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from plutus import config

log = config.get_logger("chainrpc")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
RPS = float(os.environ.get("PLUTUS_RPC_RPS") or 4.0)
TIMEOUT = 45
ATTEMPTS = 4
FIRST_SPAN = 2_000_000          # blocks per getLogs to start with; adapts to what the node allows
MAX_SPAN = 4_000_000
TIME_BATCH = 20                 # blocks per batched timestamp request
# Each call INSIDE a batch counts against the node's limit (measured: ~10 items/s sustained;
# 50-item batches every 2s were refused, every 5s were fine). The pacer charges a batch by size.
EXACT_TIMES = 120               # up to this many distinct blocks: every block time fetched exactly
# 429 handling (production, 2026-09-27: a 476-wallet onboarding drew 429s on eth_getLogs and
# the old fixed 2-8s retries gave up, abandoning ~6.5M blocks). Exponential per host, honouring
# Retry-After, shared across processes through the pacer; the backfill keeps what it finished.
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 120.0
RETRY_AFTER_CAP_S = 600.0
RATE_ATTEMPTS = 8               # 429s tolerated for one call before giving the tick up
MIN_RATE_SPAN = 10_000          # below this, a throttled range is not narrowed further
RATE_STREAK_MAX = 8             # consecutive throttled ranges before the tick gives up
FLUSH_LOGS = 3_000              # a backfill hands finished blocks over at least this often...
FLUSH_S = 30.0                  # ...or this many seconds, so an interruption loses little
GROW_BELOW = 2_500              # widen the range while pages come back this small
SHRINK_ABOVE = 7_500            # narrow it before pages approach the 10,000-log cap


class RpcError(RuntimeError):
    pass


class RangeTooLarge(RpcError):
    """The node refused the block range (too many logs, or too slow). Ask for fewer blocks."""


class StateUnavailable(RpcError):
    """The node no longer holds state at that block."""


class Unreachable(RpcError):
    """Network errors, 429s or 5xx on every attempt: try another endpoint, or later."""


class RateLimited(RangeTooLarge):
    """eth_getLogs kept drawing 429s. Narrower ranges are cheaper for the node, so the reader
    shrinks the range (down to MIN_RATE_SPAN) and carries on after the back-off."""


@dataclass(frozen=True)
class Endpoint:
    url: str

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc or self.url


def endpoints(chain: str) -> list[Endpoint]:
    """PLUTUS_RPC_<CHAIN> (comma-separated) overrides the chain's configured public RPC.

    PLUTUS_RPC_DISABLE=1 forces none: tests set it so no simulation can reach a real node.
    """
    if (os.environ.get("PLUTUS_RPC_DISABLE") or "").strip() in ("1", "true", "yes"):
        return []
    env = (os.environ.get(f"PLUTUS_RPC_{chain.upper()}") or "").strip()
    try:
        urls = [u.strip() for u in env.split(",") if u.strip()] if env else \
            list(config.chain(chain).rpc_urls)
    except KeyError:
        return []
    return [Endpoint(u) for u in urls]


def available(chain: str) -> bool:
    return bool(endpoints(chain))


def confirmations(chain: str) -> int:
    try:
        return int(config.chain(chain).confirm_blocks)
    except KeyError:
        return 12


# ── pacing, shared across processes ─────────────────────────────────────────────────
_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()
_local = threading.Lock()
_calls = 0
_calls_lock = threading.Lock()


def calls() -> int:
    """Requests sent by this process so far (the tracker reports its delta per tick)."""
    return _calls


def _db() -> sqlite3.Connection:
    global _conn
    with _conn_lock:
        if _conn is None:
            c = sqlite3.connect(str(config.DB_PATH), timeout=10, check_same_thread=False,
                                isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=10000")
            c.execute("CREATE TABLE IF NOT EXISTS rate_state (k TEXT PRIMARY KEY, next_free REAL NOT NULL)")
            _conn = c
        return _conn


def _pace(ep: Endpoint, weight: int = 1) -> None:
    key = "rpc:" + ep.host
    wait = 0.0
    with _local:
        try:
            c = _db()
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = c.execute("SELECT next_free FROM rate_state WHERE k=?", (key,)).fetchone()
            nxt = float(row[0]) if row else 0.0
            if nxt > now + RETRY_AFTER_CAP_S + 60:     # clock skew / a stale row, not a pause
                nxt = now
            start = max(now, nxt)
            c.execute("INSERT INTO rate_state (k, next_free) VALUES (?, ?) ON CONFLICT(k) "
                      "DO UPDATE SET next_free=excluded.next_free", (key, start + weight / RPS))
            c.execute("COMMIT")
            wait = start - time.time()
        except sqlite3.Error:
            try:
                _db().execute("ROLLBACK")
            except sqlite3.Error:
                pass
            wait = 1.0 / RPS
    if wait > 0:
        time.sleep(wait)


_backoff: dict[str, int] = {}                           # per host: consecutive 429 level


def _retry_after(r) -> float | None:
    """Retry-After as seconds -- either form (delta-seconds or an HTTP date)."""
    v = (getattr(r, "headers", None) or {}).get("Retry-After")
    if not v:
        return None
    try:
        return max(0.0, min(RETRY_AFTER_CAP_S, float(v)))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return max(0.0, min(RETRY_AFTER_CAP_S, parsedate_to_datetime(v).timestamp() - time.time()))
    except (TypeError, ValueError, IndexError):
        return None


def _throttled(ep: Endpoint, r=None) -> float:
    """Record a 429 for this host and hold EVERY caller of it: Retry-After if the node gave one,
    else 2s, 4s, 8s ... capped at BACKOFF_CAP_S. Returns the wait."""
    level = _backoff.get(ep.host, 0)
    _backoff[ep.host] = level + 1
    wait = _retry_after(r) if r is not None else None
    if wait is None:
        wait = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** level))
    _pause(ep, wait)
    log.info("%s throttled (429): holding all calls to it for %.0fs", ep.host, wait)
    return wait


def _eased(ep: Endpoint) -> None:
    if _backoff.get(ep.host):
        _backoff[ep.host] -= 1                          # step down, not reset: no instant re-burst


def _pause(ep: Endpoint, seconds: float) -> None:
    try:
        _db().execute("INSERT INTO rate_state (k, next_free) VALUES (?, ?) ON CONFLICT(k) "
                      "DO UPDATE SET next_free=MAX(next_free, excluded.next_free)",
                      ("rpc:" + ep.host, time.time() + seconds))
    except sqlite3.Error:
        pass


# ── one request ─────────────────────────────────────────────────────────────────────
_RANGE = ("exceeds limit", "timed out", "timeout", "too many", "block range", "range",
          "query returned more than", "response size")
_STATE = ("historical state", "missing trie node", "unknown state", "state is not available",
          "header not found")


def _post(ep: Endpoint, method: str, params: list) -> Any:
    """One call. Network errors and 5xx retry ATTEMPTS times; 429s back off exponentially up to
    RATE_ATTEMPTS times -- except eth_getLogs, which after two 429s raises RateLimited so the
    reader can narrow its range while the back-off runs."""
    global _calls
    last = ""
    failures = throttles = 0
    while failures < ATTEMPTS and throttles < RATE_ATTEMPTS:
        _pace(ep)
        with _calls_lock:
            _calls += 1
        try:
            r = requests.post(ep.url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                            "params": params}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            failures += 1
            last = f"network: {str(exc)[:100]}"
            time.sleep(min(8.0, 1.0 * failures))
            continue
        rate_msg = None
        if r.status_code == 429:
            rate_msg = "http 429"
        elif r.status_code >= 500:
            failures += 1
            last = f"http {r.status_code}"
            _pause(ep, min(30.0, 2.0 * 2 ** (failures - 1)))
            continue
        else:
            try:
                j = r.json()
            except ValueError:
                failures += 1
                last = f"not JSON (http {r.status_code})"
                time.sleep(1.0 * failures)
                continue
            if not isinstance(j, dict):
                failures += 1
                last = "unexpected response shape"
                continue
            if "error" in j:
                msg = str((j["error"] or {}).get("message") if isinstance(j["error"], dict)
                          else j["error"])[:200]
                low = msg.lower()
                if ("rate" in low and "limit" in low) or "too many requests" in low:
                    rate_msg = f"rate limited: {msg}"
                elif any(s in low for s in _STATE):
                    raise StateUnavailable(f"{ep.host}: {msg}")
                elif method == "eth_getLogs" and any(s in low for s in _RANGE):
                    raise RangeTooLarge(f"{ep.host}: {msg}")
                else:
                    raise RpcError(f"{ep.host} {method}: {msg}")
            elif "result" not in j:
                failures += 1
                last = "no result in response"
                continue
            else:
                _eased(ep)
                return j["result"]
        throttles += 1
        wait = _throttled(ep, r)
        last = f"{rate_msg} (backed off {wait:.0f}s)"
        if method == "eth_getLogs" and throttles >= 2:
            raise RateLimited(f"{ep.host}: {last}")
    raise Unreachable(f"{ep.host} {method} failed after {failures + throttles} attempts ({last})")


def _post_batch(ep: Endpoint, method: str, params_list: list[list]) -> list[Any]:
    """One HTTP request carrying several calls. Results align with `params_list`; a call the
    node answered with an error comes back as None. The node counts every item against its
    rate, so a 429 backs off like any other; three in a row raise RangeTooLarge to shrink it."""
    global _calls
    last = ""
    body = [{"jsonrpc": "2.0", "id": k, "method": method, "params": ps}
            for k, ps in enumerate(params_list)]
    refused = 0
    for attempt in range(1, ATTEMPTS + 1):
        _pace(ep, len(body))
        with _calls_lock:
            _calls += 1
        try:
            r = requests.post(ep.url, json=body, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last = f"network: {str(exc)[:100]}"
            time.sleep(min(8.0, 1.0 * attempt))
            continue
        if r.status_code == 429:
            refused += 1
            last = f"http 429 (backed off {_throttled(ep, r):.0f}s)"
            if refused >= 3:
                raise RangeTooLarge(f"{ep.host}: batch of {len(body)} throttled {refused} times")
            continue
        if r.status_code >= 500:
            last = f"http {r.status_code}"
            _pause(ep, 2.0 * attempt)
            continue
        try:
            j = r.json()
        except ValueError:
            last = f"not JSON (http {r.status_code})"
            continue
        if not isinstance(j, list):
            last = "batch answered with a single object"
            continue
        _eased(ep)
        out: list[Any] = [None] * len(params_list)
        for item in j:
            if isinstance(item, dict) and isinstance(item.get("id"), int)                     and 0 <= item["id"] < len(out) and "result" in item:
                out[item["id"]] = item["result"]
        return out
    raise Unreachable(f"{ep.host} batch {method} failed after {ATTEMPTS} attempts ({last})")


def block_times(chain: str, blocks: list[int]) -> dict[int, int]:
    """Unix time of each block. RPC logs carry no usable timestamp (measured: blockTimestamp is
    0x0), and the holder view needs one -- "holding since", "last active".

    A few blocks (every incremental sync) are fetched exactly. A backfill touches thousands, and
    fetching each at the node's ~10/s would take half an hour, so it fetches ANCHORS instead --
    the first and last block and one every `time_anchor_blocks` -- and interpolates between them.
    Measured on Robinhood chain (steady 0.1s blocks): anchors an hour apart put every
    interpolated time within ~1 second. The balances never depend on these times, only the
    "holding since" and "last active" dates do.
    """
    want = sorted(set(blocks))
    if len(want) <= EXACT_TIMES:
        fetch = want
    else:
        gap = max(1, _anchor_gap(chain))
        anchors = {want[0], want[-1]}
        x = want[0]
        while x < want[-1]:
            anchors.add(x)
            x += gap
        fetch = sorted(anchors)
    got = _fetch_times(chain, fetch)
    lost = [b for b in fetch if b not in got]
    if lost:
        log.warning("%d of %d block times could not be fetched; interpolated instead",
                    len(lost), len(fetch))
    if not got:
        if want:
            log.warning("no block times returned for %d block(s); their dates are left unknown",
                        len(want))
        return {}
    known = sorted(got)
    import bisect
    out = dict(got)
    for b in want:
        if b in out:
            continue
        k = bisect.bisect_left(known, b)
        lo = known[k - 1] if k > 0 else None
        hi = known[k] if k < len(known) else None
        if lo is not None and hi is not None and hi != lo:
            out[b] = round(got[lo] + (got[hi] - got[lo]) * (b - lo) / (hi - lo))
        else:
            out[b] = got[lo if lo is not None else hi]
    return out


def _anchor_gap(chain: str) -> int:
    try:
        return int(config.chain(chain).time_anchor_blocks)
    except KeyError:
        return 300


def _fetch_times(chain: str, want: list[int]) -> dict[int, int]:
    got: dict[int, int] = {}
    for ep in endpoints(chain):
        todo = [b for b in want if b not in got]        # a lagging node lacks the newest blocks:
        if not todo:                                    # whatever it could not give, ask the next
            break
        size, pos = TIME_BATCH, 0
        try:
            while pos < len(todo):
                chunk = todo[pos:pos + size]
                try:
                    res = _post_batch(ep, "eth_getBlockByNumber", [[hex(b), False] for b in chunk])
                except RangeTooLarge:
                    if size == 1:
                        break
                    size = max(1, size // 2)
                    continue
                for b, blk in zip(chunk, res):
                    if isinstance(blk, dict) and blk.get("timestamp"):
                        try:
                            got[b] = _hex(blk["timestamp"])
                        except RpcError:
                            pass
                pos += len(chunk)
        except Unreachable as exc:
            log.warning("%s", exc)
    return got


def _hex(v: Any) -> int:
    if not isinstance(v, str) or not v.startswith("0x"):
        raise RpcError(f"expected a hex quantity, got {str(v)[:60]!r}")
    return int(v, 16) if v != "0x" else 0


def _first(chain: str, method: str, params: list) -> tuple[Endpoint, Any]:
    eps = endpoints(chain)
    if not eps:
        raise RpcError(f"no RPC endpoint configured for {chain}")
    errors = []
    for ep in eps:
        try:
            return ep, _post(ep, method, params)
        except Unreachable as exc:
            errors.append(str(exc))
    raise Unreachable("; ".join(errors)[:400])


# ── the calls the ledger makes ──────────────────────────────────────────────────────
def _raw_head(ep: Endpoint) -> int:
    return _hex(_post(ep, "eth_blockNumber", []))


def latest_block(chain: str) -> int:
    """The newest block the ledger may sync to: the head less a confirmation margin."""
    _ep, res = _first(chain, "eth_blockNumber", [])
    return max(0, _hex(res) - confirmations(chain))


def decimals(chain: str, token: str) -> int:
    _ep, res = _first(chain, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
    return _hex(res)


def token_supply(chain: str, token: str, block: int | None = None) -> int:
    """Total supply in raw units, exact -- AT `block` when given, so it matches a ledger synced
    to that block. Raises StateUnavailable if no endpoint still holds that block's state."""
    tag = hex(block) if block is not None else "latest"
    eps = endpoints(chain)
    if not eps:
        raise RpcError(f"no RPC endpoint configured for {chain}")
    errors = []
    for ep in eps:
        try:
            return _hex(_post(ep, "eth_call", [{"to": token, "data": "0x18160ddd"}, tag]))
        except (Unreachable, StateUnavailable) as exc:
            errors.append(exc)
    if all(isinstance(e, StateUnavailable) for e in errors):
        raise StateUnavailable("; ".join(map(str, errors))[:400])
    raise Unreachable("; ".join(map(str, errors))[:400])


def _parse(r: dict) -> dict | None:
    topics = r.get("topics") or []
    if len(topics) != 3 or (topics[0] or "").lower() != TRANSFER_TOPIC:
        return None                                     # not an ERC-20 Transfer (e.g. ERC-721)
    if r.get("removed"):
        return None                                     # reorged out
    data = r.get("data") or "0x"
    return {"block": _hex(r["blockNumber"]), "log_index": _hex(r["logIndex"]),
            "tx_hash": str(r["transactionHash"]).lower(),
            "from": "0x" + topics[1][-40:].lower(), "to": "0x" + topics[2][-40:].lower(),
            "raw": int(data, 16) if data not in ("0x", "") else 0,
            "ts": _hex(r["blockTimestamp"]) if r.get("blockTimestamp") else 0}


_span: dict[str, int] = {}                              # per host: the range that last worked


def transfer_logs(chain: str, token: str, from_block: int, to_block: int,
                  on_chunk=None) -> list[dict]:
    """Every Transfer of `token` in [from_block, to_block], oldest first, deduplicated.

    Ranges adapt: a refusal (too many logs, too slow) halves the range and retries; small pages
    widen it; persistent 429s narrow it and back off. An endpoint is only asked about blocks it
    has itself reached -- a node that is behind answers an empty list for blocks it does not
    have, which would lose transfers silently.

    With `on_chunk(logs, through_block)`, finished work is handed over as it completes (every
    FLUSH_LOGS logs or FLUSH_S seconds, and at the end): `through_block` is fully covered, so the
    caller can commit it and a later call can resume after it. The return value is then [].
    Without it, everything is returned at the end. Either way, if the range cannot be finished
    this RAISES -- after handing over what was finished -- and never reports a partial range as
    complete.
    """
    out: list[dict] = []
    pending: list[dict] = []
    seen: set[tuple[str, int]] = set()
    eps = endpoints(chain)
    if not eps:
        raise RpcError(f"no RPC endpoint configured for {chain}")
    i = 0
    heads: dict[str, int] = {}
    cursor = from_block
    flushed_at = time.time()
    throttled_streak = 0

    def flush(through: int) -> None:
        nonlocal pending, flushed_at
        pending.sort(key=lambda x: (x["block"], x["log_index"]))
        if pending:
            times = block_times(chain, [x["block"] for x in pending])
            if not times:
                raise Unreachable("block times unavailable; not committing these blocks yet")
            for x in pending:
                x["ts"] = times.get(x["block"], 0)
        batch, pending = pending, []
        flushed_at = time.time()
        if on_chunk is not None:
            on_chunk(batch, through)
        else:
            out.extend(batch)

    try:
        while cursor <= to_block:
            if i >= len(eps):
                raise Unreachable(f"no endpoint could serve blocks {cursor:,}-{to_block:,}")
            ep = eps[i]
            span = _span.get(ep.host, FIRST_SPAN)
            try:
                if ep.url not in heads:
                    heads[ep.url] = _raw_head(ep)
                if heads[ep.url] < to_block:
                    heads[ep.url] = _raw_head(ep)       # the head moves every 0.1s; ask again
                    if heads[ep.url] < to_block:
                        log.info("%s is behind (head %d < %d); trying the next endpoint",
                                 ep.host, heads[ep.url], to_block)
                        i += 1
                        continue
                hi = min(to_block, cursor + span - 1)
                rows = _post(ep, "eth_getLogs", [{"address": token, "topics": [TRANSFER_TOPIC],
                                                  "fromBlock": hex(cursor), "toBlock": hex(hi)}])
            except RateLimited as exc:
                throttled_streak += 1
                if throttled_streak > RATE_STREAK_MAX:
                    raise Unreachable(f"{exc} -- still throttled after {throttled_streak} "
                                      f"narrower tries; resuming from block {cursor:,} later")
                _span[ep.host] = max(min(span, MIN_RATE_SPAN), span // 2)
                continue
            except RangeTooLarge:
                if span <= 1:
                    raise RpcError(f"block {cursor:,} alone holds more transfers than {ep.host} "
                                   f"will return")
                _span[ep.host] = max(1, span // 2)
                continue
            except Unreachable as exc:
                log.warning("%s", exc)
                i += 1
                continue
            throttled_streak = 0
            if not isinstance(rows, list):
                raise RpcError(f"{ep.host} eth_getLogs returned {type(rows).__name__}, not a list")
            for r in rows:
                p = _parse(r)
                if p is None or not (cursor <= p["block"] <= hi):
                    continue
                k = (p["tx_hash"], p["log_index"])
                if k not in seen:
                    seen.add(k)
                    pending.append(p)
            cursor = hi + 1
            if len(rows) < GROW_BELOW:
                _span[ep.host] = min(MAX_SPAN, span * 2)
            elif len(rows) > SHRINK_ABOVE:
                _span[ep.host] = max(1, span // 2)
            if on_chunk is not None and cursor <= to_block and (
                    len(pending) >= FLUSH_LOGS or time.time() - flushed_at >= FLUSH_S):
                flush(cursor - 1)
    except RpcError:
        # Keep what is finished: every block before `cursor` is complete.
        if on_chunk is not None and pending and cursor > from_block:
            try:
                flush(cursor - 1)
            except RpcError:
                pass                                    # it will be fetched again next time
        raise
    flush(to_block)
    return out
