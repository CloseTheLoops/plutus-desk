"""The four trackers. Each owns its cadence and its persistence; none of them decide anything.

    pool       reserves + price, every 30-60s          1 call
    tape       fills, SPLIT ours/third ON INGEST       1 call, free
    inventory  our balances, DIRECT per-wallet         delta-driven, see below
    census     third-party holders, union of slices    35 calls, hourly

INVENTORY IS THE ONE WORTH READING ABOUT. A full pass over ~150 wallets took ~85s at roughly half a
second per call with zero failures — fine once, ruinous every tick (~15k calls/day for a
number that barely moves). The tape already names every address that traded, so the delta pass
re-queries only the intersection of (our wallets) x (addresses seen trading), and a full
reconciliation runs hourly. That is a few hundred calls/day instead of ~15k — and it is the same
measurement that made capture-rate possible, paying for itself twice.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from plutus import config, db
from plutus.sources import gecko, gmgn

log = config.get_logger("track")


@dataclass
class TickResult:
    name: str
    ok: bool
    calls: int = 0
    seconds: float = 0.0
    detail: str = ""


def _ctx(token_id: int) -> tuple[str, str, str]:
    t = db.token_row(token_id)
    if t is None:
        raise ValueError(f"unknown token {token_id}")
    return t["chain"], t["address"], (t["primary_venue"] or "")


# ── pool ──────────────────────────────────────────────────────────────────────
def track_pool(token_id: int) -> TickResult:
    if aborted(token_id):
        return TickResult("pool", False, 0, 0.0,
                          "skipped — the token was deleted")
    t0 = time.time()
    chain, address, primary = _ctx(token_id)
    try:
        p = gmgn.token_pool(chain, address)
    except gmgn.GmgnError as exc:
        return TickResult("pool", False, 1, time.time() - t0, str(exc)[:120])
    base = float(p.get("base_reserve") or 0)
    quote = float(p.get("quote_reserve") or 0)
    if not (base and quote):
        return TickResult("pool", False, 1, time.time() - t0, "empty reserves")
    db.record_pool(token_id, primary or p.get("pool_address") or "?", base, quote,
                   float(p.get("liquidity") or 0), "gmgn")
    return TickResult("pool", True, 1, time.time() - t0,
                      f"R {base:,.0f} Q {quote:,.2f} spot {quote/base:.6e}")


# ── tape ──────────────────────────────────────────────────────────────────────
def track_tape(token_id: int) -> TickResult:
    """Ingest fills. `is_ours` is decided HERE, once, and never recomputed at display time."""
    if aborted(token_id):
        return TickResult("tape", False, 0, 0.0,
                          "skipped — the token was deleted")
    t0 = time.time()
    chain, address, primary = _ctx(token_id)
    if not primary:
        return TickResult("tape", False, 0, 0.0, "no primary venue yet — run onboarding")
    net = config.chain(chain).gecko_network
    fills = gecko.trades(net, primary)
    ours = {a for a, c in db.class_map(token_id).items() if c == "ours"}

    rows, mine = [], 0
    for f in fills:
        if not f["tx_hash"]:
            continue
        maker = config.norm_addr(chain, f["maker"]) if f["maker"] else ""
        is_ours = maker in ours
        mine += is_ours
        rows.append((token_id, f["tx_hash"], 0, primary, f["ts"], f["side"],
                     f["usd"], f.get("base_amount") or
                     (f["to_amount"] if f["side"] == "buy" else f["from_amount"]),
                     f["price_usd"], maker, int(is_ours), "geckoterminal"))
    new = db.record_trades(rows)
    return TickResult("tape", True, 1, time.time() - t0,
                      f"{len(rows)} fills seen, {new} new, {mine} ours")


# ── inventory ─────────────────────────────────────────────────────────────────
# How many balance reads run at once.  MEASURED, not guessed: one call costs ~0.54s, almost
# all of it spawning a Node process, so 155 wallets take ~84s in a plain loop. A pool of 8
# ran the same work 7.2x faster. The global pacer still caps the process at
# 1/MIN_INTERVAL_S calls per second, so raising this trades latency for nothing once the
# pacer binds -- it is deliberately well under that ceiling.
BALANCE_WORKERS = 8

# ── stopping work that is already in flight ───────────────────────────────────
# WHY A FLAG AND NOT A CANCEL. Every long scan runs under `asyncio.to_thread`, and cancelling
# the awaiting task does NOT interrupt the thread -- it runs to completion regardless. So a
# 155-wallet sweep for a token that has just been deleted keeps making all 155 calls against a
# row that no longer exists. Task cancellation cannot reach it; a flag the scan checks between
# calls can.
#
# Checked at every API-call boundary, so an abort costs at most one more call, never a whole
# sweep.
_ABORTED: set[int] = set()
_ABORT_LOCK = threading.Lock()


def abort(token_id: int) -> None:
    """Tell every in-flight scan for this token to stop at its next call boundary."""
    with _ABORT_LOCK:
        _ABORTED.add(token_id)


def aborted(token_id: int) -> bool:
    with _ABORT_LOCK:
        return token_id in _ABORTED


def clear_abort(token_id: int) -> None:
    """Re-onboarding an id that was previously deleted must not inherit its abort."""
    with _ABORT_LOCK:
        _ABORTED.discard(token_id)


def track_inventory(token_id: int, full: bool = False, window_s: int = 3600,
                    progress=None) -> TickResult:
    """Our own holdings, by DIRECT per-wallet query. Never inferred from a ranked sweep —
    on the first token a ranked sweep saw barely a third of the operator's wallets; the direct query found all."""
    t0 = time.time()
    chain, address, _ = _ctx(token_id)
    cmap = db.class_map(token_id)
    ours = sorted(a for a, c in cmap.items() if c == "ours")
    # THE LEDGER NEEDS A BALANCE FOR EVERY CLASS, not just ours. pool/burnt/locked are a handful
    # of contracts and an unobserved one reads as zero, which makes the ledger silently wrong in
    # the most dangerous direction: it inflates the float residual.
    others = sorted(a for a, c in cmap.items() if c in ("pool", "burnt", "locked", "unknown"))
    if not ours and not others:
        return TickResult("inventory", False, 0, 0.0, "nothing classified yet — run onboarding")

    if full:
        targets = ours + others
    else:
        seen = {r["maker"] for r in db.connect().execute(
            "SELECT DISTINCT maker FROM trades WHERE token_id=? AND ts>=?",
            (token_id, db.now() - window_s)).fetchall() if r["maker"]}
        known = set(db.latest_balances(token_id))
        # the pool moves on every fill, so it is always re-read; the rest only when they traded
        targets = sorted((set(ours) & seen) | (set(ours) - known)
                         | {a for a in others if cmap[a] == "pool"}
                         | (set(others) - known))

    skipped = [w for w in targets if not config.is_address(chain, w)]
    targets = [w for w in targets if config.is_address(chain, w)]
    # CONCURRENT, because the cost here is process startup, not rate limit. Sequentially this
    # is ~0.54s per wallet and the operator watches a blank desk for a minute and a half while
    # every derived figure reads zero. gmgn._pace() still enforces the global interval across
    # these threads, so the burst ceiling is unchanged.
    rows, calls, failed = [], 0, []

    def _read(w: str):
        # Checked per wallet, not per sweep: a delete part-way through stops the rest rather
        # than paying for every remaining wallet on a token that is gone.
        if aborted(token_id):
            return w, None, "aborted"
        try:
            # `full` is the operator pressing "full pull". That is an explicit request for
            # current numbers, so it goes past the cache; the background delta sweep does not.
            return w, gmgn.token_balance(chain, w, address, fresh=full), None
        except gmgn.GmgnError as exc:
            return w, None, exc

    done, skipped = 0, 0
    with ThreadPoolExecutor(max_workers=min(BALANCE_WORKERS, max(1, len(targets)))) as ex:
        for w, got, exc in ex.map(_read, targets):
            done += 1
            if exc == "aborted":
                skipped += 1
                continue
            if progress:
                progress(done, len(targets))
            if exc is not None:
                # A wallet we could not read is NOT a wallet holding nothing. It never reaches
                # record_balances, so ledger falls back to 0.0 and the operator sees an empty
                # desk. Count them and fail the tick, or the UI reports a clean sweep over
                # missing data.
                failed.append(w)
                log.warning("balance failed for %s: %s", w[:10], exc)
            else:
                calls += 1
                rows.append((w, got[0], got[1]))
    if skipped:
        log.info("inventory for token %s aborted — %d wallet(s) never queried", token_id, skipped)
        return TickResult("inventory", False, calls, time.time() - t0,
                          f"ABORTED after {calls} of {len(targets)} wallets — {skipped} calls "
                          f"not made because the token was deleted")
    db.record_balances(token_id, rows)
    ok = not failed
    return TickResult("inventory", ok, calls, time.time() - t0,
                      f"{'FULL' if full else 'delta'} · {len(rows)} of "
                      f"{len(ours)+len(others)} addresses ({calls} calls, {time.time()-t0:.1f}s)"
                      + (f" · skipped {len(skipped)} non-address ids" if skipped else "")
                      + (f" · {len(failed)} BALANCE READS FAILED — those wallets are unread, "
                         f"not empty" if failed else ""))


# ── census ────────────────────────────────────────────────────────────────────
ORDER_BYS = ("amount_percentage", "profit", "unrealized_profit", "buy_volume_cur", "sell_volume_cur")
TAGS = ("smart_degen", "renowned", "fresh_wallet", "dev", "sniper", "rat_trader",
        "bundler", "transfer_in", "dex_bot", "bluechip_owner")
TAG_ORDER_BYS = ("amount_percentage", "profit", "buy_volume_cur")


def track_census(token_id: int) -> TickResult:
    """Union of many ranked slices, because each caps at 100 rows.

    COVERAGE IS STATED, NEVER ASSUMED: a wallet missing from every slice is UNCOVERED, not
    absent-and-therefore-fine. Per-slice new-address yield is logged so a slice that adds
    nothing is visible rather than presumed useful.
    """
    t0 = time.time()
    chain, address, _ = _ctx(token_id)
    rows: dict[str, dict] = {}
    tagged: dict[str, set[str]] = {}
    calls = slices = 0

    def absorb(lst: list[dict], tag: str | None) -> int:
        new = 0
        for w in lst:
            a = config.norm_addr(chain, w.get("address") or "")
            if not a:
                continue
            if a not in rows:
                new += 1
            prev = rows.get(a)
            if prev is None or sum(v is not None for v in w.values()) > prev["_n"]:
                rows[a] = {**w, "_n": sum(v is not None for v in w.values())}
            if tag:
                tagged.setdefault(a, set()).add(tag)
        return new

    for ob in ORDER_BYS:
        if aborted(token_id):
            log.info("census for token %s aborted before slice %s", token_id, ob)
            return TickResult("census", False, calls, time.time() - t0,
                              f"ABORTED after {calls} slices — the token was deleted")
        try:
            n = absorb(gmgn.traders(chain, address, order_by=ob), None)
            calls += 1
            slices += 1
            log.debug("census order-by %s: +%d new (union %d)", ob, n, len(rows))
        except gmgn.GmgnError as exc:
            log.warning("census slice order-by %s FAILED: %s", ob, exc)
    for tg in TAGS:
        if aborted(token_id):
            log.info("census for token %s aborted at tag %s (%d slices done)",
                     token_id, tg, slices)
            return TickResult("census", False, calls, time.time() - t0,
                              f"ABORTED after {calls} calls — the token was deleted")
        for ob in TAG_ORDER_BYS:
            try:
                absorb(gmgn.traders(chain, address, order_by=ob, tag=tg), tg)
                calls += 1
                slices += 1
            except gmgn.GmgnError:
                pass

    if not rows:
        return TickResult("census", False, calls, time.time() - t0, "no rows returned")

    sweep = db.now()
    info = {}
    try:
        info = gmgn.token_info(chain, address)
        calls += 1
    except gmgn.GmgnError:
        pass

    def f(w, k):
        try:
            return float(w.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    db.connect().executemany(
        "INSERT OR IGNORE INTO census VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(token_id, sweep, a, f(w, "balance"), f(w, "amount_percentage"), f(w, "usd_value"),
          f(w, "avg_cost"), f(w, "realized_profit"), f(w, "unrealized_profit"),
          int(f(w, "start_holding_at")), int(f(w, "last_active_timestamp")),
          int(bool(w.get("is_new"))), int(bool(w.get("is_suspicious"))),
          int(bool(w.get("transfer_in"))), ",".join(sorted(tagged.get(a, ()))),
          int(w.get("addr_type") or 0))
         for a, w in rows.items()])
    holder_count = int(info.get("holder_count") or 0)
    db.connect().execute("INSERT OR REPLACE INTO census_meta VALUES (?,?,?,?,?,?,?)",
                         (token_id, sweep, slices, len(rows), holder_count, calls,
                          round(time.time() - t0, 1)))
    db.connect().commit()

    cov = f"{len(rows)}/{holder_count} ({len(rows)/holder_count:.0%})" if holder_count else f"{len(rows)}"
    return TickResult("census", True, calls, time.time() - t0,
                      f"{slices} slices, covered {cov}")


def tick(token_id: int, full_inventory: bool = False, with_census: bool = False) -> list[TickResult]:
    out = [track_pool(token_id), track_tape(token_id),
           track_inventory(token_id, full=full_inventory)]
    if with_census:
        out.append(track_census(token_id))
    return out
