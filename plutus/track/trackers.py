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

import math
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from plutus import config, db
from plutus.sources import etherscan, gecko, gmgn, transfers

log = config.get_logger("track")


@dataclass
class TickResult:
    name: str
    ok: bool
    calls: int = 0
    seconds: float = 0.0
    detail: str = ""
    # When a step was held back for budget: the time its calls will be affordable again.
    resume_at: float | None = None
    # What actually happened, for the page. `ok` alone showed a budget DEFERRAL as "FAILED":
    # ok | partial (some read, rest queued) | deferred (none yet, all queued) | skipped | failed
    status: str | None = None
    done: int | None = None
    of: int | None = None


def _ctx(token_id: int) -> tuple[str, str, str]:
    t = db.token_row(token_id)
    if t is None:
        raise ValueError(f"unknown token {token_id}")
    return t["chain"], t["address"], (t["primary_venue"] or "")


# ── pool ──────────────────────────────────────────────────────────────────────
def track_pool(token_id: int, verify: bool = False, background: bool = False) -> TickResult:
    if aborted(token_id):
        return TickResult("pool", False, 0, 0.0,
                          "skipped — the token was deleted")
    t0 = time.time()
    chain, address, primary = _ctx(token_id)

    # WHY NOT JUST SWITCH TO THE FREE SOURCE. Reading reserves from GeckoTerminal costs nothing
    # against the GMGN budget, which is what keeps a campaign's prices live while a big wallet
    # sweep is spending that budget. But the free source gives a pool's USD value, not its
    # reserves, and deriving reserves from it is only right on a full-range pool. Measured on
    # the same day: FAITH within 1%, another Robinhood token off by 150-240%. So each token is
    # CHECKED against GMGN -- on first use and hourly -- and reads from the free source only
    # while that check passes. A token that fails keeps reading GMGN exactly as before.
    free = None
    if primary:
        try:
            free = gecko.pool(config.chain(chain).gecko_network, primary)
        except Exception as exc:                          # noqa: BLE001 — the free source is optional
            log.debug("free pool read failed for token %s: %s", token_id, exc)

    last = db.latest_pool_parity(token_id)
    trusted = bool(last and last["ok"])
    fresh_check = bool(last and db.now() - last["ts"] <= PARITY_TTL_S)
    need_check = free is not None and (verify or not fresh_check)

    vendor, calls, note = None, 0, ""
    if need_check or not (free is not None and trusted):
        try:
            vendor = gmgn.token_pool(chain, address, fresh=need_check, background=background)
            calls += 1
        except gmgn.GmgnError as exc:
            if not (free is not None and trusted):
                return TickResult("pool", False, calls, time.time() - t0, str(exc)[:120])
            note = f" · could not re-check against GMGN ({str(exc)[:60]}), kept the free source"

    if need_check and vendor:
        vr, vq = float(vendor.get("base_reserve") or 0), float(vendor.get("quote_reserve") or 0)
        if vr and vq:
            rd = free["base_reserve"] / vr - 1
            qd = free["quote_reserve"] / vq - 1
            sd = free["spot"] / (vq / vr) - 1
            ok = all(abs(x) <= PARITY_TOLERANCE for x in (rd, qd, sd))
            db.record_pool_parity(token_id, rd, qd, sd, ok)
            trusted = ok
            if not ok:
                log.info("token %s: free pool source disagrees with GMGN (R %+.1f%% Q %+.1f%% "
                         "spot %+.1f%%) — staying on GMGN", token_id, rd * 100, qd * 100, sd * 100)

    if free is not None and trusted:
        base, quote, liq, source = (free["base_reserve"], free["quote_reserve"],
                                    free["reserve_usd"], "gecko")
    elif vendor:
        base = float(vendor.get("base_reserve") or 0)
        quote = float(vendor.get("quote_reserve") or 0)
        liq, source = float(vendor.get("liquidity") or 0), "gmgn"
    else:
        return TickResult("pool", False, calls, time.time() - t0, "no usable pool source")
    if not (base and quote):
        return TickResult("pool", False, calls, time.time() - t0, "empty reserves")
    db.record_pool(token_id, primary or (vendor or {}).get("pool_address") or "?", base, quote,
                   liq, source)
    return TickResult("pool", True, calls, time.time() - t0,
                      f"R {base:,.0f} Q {quote:,.2f} spot {quote/base:.6e} via {source}" + note)


# ── the transfer ledger ──────────────────────────────────────────────────────────
# How often the ledger is re-checked against total supply. One call; a mismatch rebuilds it.
LEDGER_VERIFY_S = 3600


# One ledger sync per token at a time. The onboarding scan and the tracker loop both sync, and
# two concurrent backfills of the same token doubled the RPC load that drew the 429s.
_ledger_locks: dict[int, threading.Lock] = {}
_ledger_locks_guard = threading.Lock()
# What a running sync is doing, for the page: {running, stage, from, to, block, transfers, ...}.
ledger_progress: dict[int, dict] = {}


class _Deleted(Exception):
    pass


def track_ledger(token_id: int, wait: bool = False) -> TickResult:
    """Bring the transfer ledger up to the chain head, and check it against total supply.

    The first run backfills from the token's first block; every run after fetches only blocks
    since the last. A backfill COMMITS AS IT GOES: an interruption (429s that outlast the
    back-off, a restart, a network drop) resumes from the last finished block instead of
    starting over. Balances derived from it are exact, so the hourly check is exact too: the
    holdings must sum to total supply to the last unit, or the ledger is rebuilt rather than
    trusted. Supply is read AT the synced block where the source allows (the free RPC does), so
    a transfer landing between the two reads cannot make a good ledger look wrong.

    `wait=True` waits for a sync already running for this token instead of skipping.
    """
    with _ledger_locks_guard:
        lock = _ledger_locks.setdefault(token_id, threading.Lock())
    if not lock.acquire(blocking=wait):
        return TickResult("ledger", False, 0, 0.0, "a ledger sync for this token is already "
                          "running", status="skipped")
    try:
        return _track_ledger(token_id)
    finally:
        lock.release()


def _track_ledger(token_id: int) -> TickResult:
    if aborted(token_id):
        return TickResult("ledger", False, 0, 0.0, "skipped — the token was deleted",
                          status="skipped")
    t0 = time.time()
    chain, address, _ = _ctx(token_id)
    src = transfers.for_chain(chain)
    if src is None:
        return TickResult("ledger", False, 0, 0.0,
                          "no transfer-log source for this chain (no RPC, no Etherscan key)",
                          status="skipped")
    before = src.calls()

    def used() -> int:
        return max(0, src.calls() - before)

    prog = {"running": True, "stage": "sync", "started": db.now(), "from": None, "to": None,
            "block": None, "transfers": 0, "backfill": False, "detail": ""}
    ledger_progress[token_id] = prog
    try:
        st = db.ledger_state(token_id)
        dec = st["decimals"] if st else src.decimals(address)
        start = st["synced_block"] + 1 if st else 0
        head = src.latest_block()
        prog.update(stage="backfill" if (st is None or not st["verified_ts"]) else "sync",
                    backfill=st is None or not st["verified_ts"], to=head, block=start - 1)
        prog["from"] = start
        new = 0

        def on_chunk(logs: list[dict], through: int) -> None:
            nonlocal new
            if aborted(token_id):                       # deleted mid-backfill: write nothing more
                raise _Deleted()
            new += db.apply_transfers(token_id, logs, dec, through, caught_up=through >= head)
            prog.update(block=through, transfers=new)

        if head >= start:
            src.transfer_logs(address, start, head, on_chunk=on_chunk)
        else:
            head = start - 1                            # a lagging node: nothing new, keep ours
        detail = f"synced to block {head:,} · {new} new transfer(s)"
    except _Deleted:
        prog.update(running=False, detail="the token was deleted")
        return TickResult("ledger", False, used(), time.time() - t0,
                          "skipped — the token was deleted", status="skipped")
    except transfers.LedgerSourceError as exc:
        st2 = db.ledger_state(token_id)
        kept = (f" · progress kept to block {st2['synced_block']:,}, resuming from there"
                if st2 else "")
        prog.update(running=False, detail=f"{str(exc)[:200]}{kept}")
        return TickResult("ledger", False, used(), time.time() - t0,
                          f"{src.name}: {str(exc)[:240]}{kept}", status="failed")

    prog["stage"] = "verify"
    st = db.ledger_state(token_id)
    due = (st is not None and (not st["verified_ts"] or st["verified_ok"] != 1
           or db.now() - st["verified_ts"] >= LEDGER_VERIFY_S))
    try:
        if due:
            try:
                supply = src.token_supply(address, head)
                held = db.holdings_sum_raw(token_id)
                if held != supply:
                    # A source that can only read the LATEST supply may be ahead of the ledger.
                    # Catch up once before calling it wrong.
                    head2 = src.latest_block()
                    if head2 > head:
                        src.transfer_logs(address, head + 1, head2, on_chunk=on_chunk)
                        head = head2
                        held = db.holdings_sum_raw(token_id)
                        supply = src.token_supply(address, head2)
            except _Deleted:
                return TickResult("ledger", False, used(), time.time() - t0,
                                  "skipped — the token was deleted", status="skipped")
            except transfers.LedgerSourceError as exc:
                # The sync itself worked; only the check could not run. Try again next pass --
                # the ledger stops counting as exact if checks keep failing (db.LEDGER_VERIFIED_S).
                return TickResult("ledger", True, used(), time.time() - t0,
                                  detail + f" · supply check deferred ({str(exc)[:120]})",
                                  status="ok")
            ok = held == supply
            db.set_ledger_verified(token_id, ok)
            if not ok:
                log.warning("transfer ledger for token %s is off total supply by %d raw units — "
                            "rebuilding it from the first block", token_id, supply - held)
                db.reset_ledger(token_id)
                return TickResult("ledger", False, used(), time.time() - t0,
                                  "did not reconcile to supply — rebuilding", status="failed")
            detail += " · reconciles to total supply exactly"
        return TickResult("ledger", True, used(), time.time() - t0, detail, status="ok")
    finally:
        prog.update(running=False, stage="done")


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
    last_block = db.last_trade_block(token_id)
    fills = gecko.trades(net, primary)
    ours = {a for a, c in db.class_map(token_id).items() if c == "ours"}

    # A FULL window whose oldest fill is newer than the newest one already stored means fills in
    # between were never seen -- a burst of buys faster than one poll can do exactly this. Balances
    # rolled forward from fills would then undercount, so the gap is recorded and the inventory
    # sweep re-reads the wallets whose last read predates it.
    blocks = [f["block"] for f in fills if f.get("block")]
    gap = (last_block is not None and blocks and len(fills) >= gecko.TRADES_WINDOW
           and min(blocks) > last_block)
    if gap:
        db.record_tape_gap(token_id, last_block, min(blocks))
        log.warning("tape gap on token %s: nothing seen between blocks %s and %s",
                    token_id, last_block, min(blocks))

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
                     f["price_usd"], maker, int(is_ours), "geckoterminal", f.get("block")))
    new = db.record_trades(rows)
    return TickResult("tape", True, 1, time.time() - t0, done=new, of=len(rows),
                      detail=f"{len(rows)} fills seen, {new} new, {mine} ours"
                      + (" · GAP: fills may have been missed, affected wallets will be re-read"
                         if gap else ""))


# ── inventory ─────────────────────────────────────────────────────────────────
# How many balance reads run at once.  MEASURED, not guessed: one call costs ~0.54s, almost
# all of it spawning a Node process, so 155 wallets take ~84s in a plain loop. A pool of 8
# ran the same work 7.2x faster. The global pacer still caps the process at
# 1/MIN_INTERVAL_S calls per second, so raising this trades latency for nothing once the
# pacer binds -- it is deliberately well under that ceiling.
BALANCE_WORKERS = int(os.environ.get("PLUTUS_BALANCE_WORKERS") or 4)

# ── how often our wallets are re-read at all ──────────────────────────────────
# Our position is rolled forward from our own fills, so a real read is only needed to catch what
# fills cannot see: transfers, other venues, staking. That is a slow drift, so it is caught on a
# slow ROLLING schedule -- each loop step re-reads the few stalest wallets -- not by re-reading
# every wallet at once. Re-reading them all hourly was ~460 calls an hour at 458 wallets.
RECONCILE_S = int(os.environ.get("PLUTUS_RECONCILE_S") or 12 * 3600)
# A wallet at zero with no fills since its last read has nothing to drift: at most daily.
ZERO_RECHECK_S = 24 * 3600
# Once a re-read PROVES one of our wallets moved outside the trade feed, every other wallet of ours
# read before that proof is re-read within this long. Proof is rare and the error it implies is
# real -- transfers between our own wallets double- or under-count the pair until both are read --
# so it is corrected fast; the background ceiling already bounds how fast.
URGENT_S = 3600
# A staking/locking contract jumping is WEAKER evidence: outsiders stake too. Re-read within this.
URGENT_WEAK_S = 3 * 3600
# A re-read further than this from its rolled-forward estimate is drift, not rounding.
DRIFT_TOLERANCE = 0.001
# A staking/locking contract moving by more than this share of supply between two reads.
LOCKED_JUMP = 0.0005
# The pool and staking contracts are re-read this often -- often enough to catch staking within
# minutes, not so often that two tokens spend 1,100 calls a day on four addresses.
CONTRACT_REREAD_S = 600
# ROUTINE re-reads stop once background usage passes this share of the background ceiling,
# leaving the rest for the census and for correcting PROVEN drift. Without it, two tokens' steady
# re-reads plus another process's calls filled the daily ceiling, background paused completely,
# and a known 3.4% error in our position waited behind routine work for five hours.
ROUTINE_SHARE = 0.8
# ...but routine never stops outright. Under sustained pressure it still re-reads any wallet older
# than this, so the longest a wallet can go unread is bounded. Stopping completely let wallets
# reach 32h after a heavy day, visible only as an "overdue" note.
HARD_STALE_S = 2 * RECONCILE_S
_RECONCILE_CREDIT: dict[int, float] = {}

# Calls held back from every sweep so a live campaign can still read prices and quotes when a
# large inventory sweep would otherwise spend the hour's budget.
BUDGET_RESERVE = 50

# The free pool source is trusted for a token only while its latest check against GMGN agreed
# within this much on reserves and price, and only for this long before re-checking.
PARITY_TOLERANCE = 0.03
PARITY_TTL_S = 3600

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
                    progress=None, fresh: bool = False, background: bool = False,
                    reconcile: float = 0) -> TickResult:
    """Read wallet balances. A FULL read takes a cross-process lock first.

    `reconcile` is the loop's step length in seconds: when set, the step also re-reads the few
    stalest wallets, sized so every wallet is re-read within RECONCILE_S.
    """
    if not full:
        return _inventory(token_id, full, window_s, progress, fresh, background, reconcile)
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    holder = db.acquire_scan_lock(token_id, owner)
    if holder is not None:
        return TickResult("inventory", False, 0, 0.0,
                          f"SKIPPED — a full read of this token is already running "
                          f"({holder['owner']}, {db.now() - (holder['started'] or 0)}s ago)",
                          status="skipped")
    if not background:
        db.record_full_request(token_id)
    beat = [time.time()]

    def heartbeat(done: int, of: int) -> None:
        if time.time() - beat[0] > 30:
            beat[0] = time.time()
            db.refresh_scan_lock(token_id, owner)
        if progress:
            progress(done, of)

    try:
        return _inventory(token_id, full, window_s, heartbeat, fresh, background, reconcile)
    finally:
        db.release_scan_lock(token_id, owner)


def _routine_allowed() -> bool:
    b = gmgn.budget(background=True)
    if b.get("hour") is None:
        return True
    return (b["hour"] < ROUTINE_SHARE * b["hour_cap"]
            and b["day"] < ROUTINE_SHARE * b["day_cap"])


def _reconcile_pick(token_id: int, candidates: list[str], rows: dict, step_s: float,
                    split: bool = False):
    """The wallets to re-read this step: the smallest steady rate that meets every deadline.

    Each wallet's deadline is its last read plus its limit -- RECONCILE_S normally, ZERO_RECHECK_S
    for an idle zero wallet, URGENT_S if it was read before tokens were caught moving outside the
    feed. Sorted by deadline, the rate that meets them all is the maximum over k of k divided by
    the time left until the k-th deadline. Reading that many each step, earliest deadline first,
    keeps every wallet inside its limit while spreading the reads out.

    WHY NOT "THE N STALEST, ONCE THEY ARE HALF A LIMIT OLD". A buy wave has every wallet re-read in
    the same hour; they then all come due together and a fixed N could not clear them in time --
    a simulated week had wallets 15 hours old against a 12-hour limit. Deadline order reads a few
    of that cohort early, which spreads it out for good, at the same average cost.
    """
    if not step_s:
        return ([], []) if split else []
    now = db.now()
    fills = db.fills_after(token_id, {a: (rows[a][1], rows[a][2]) for a in candidates if a in rows})
    drifted = db.drift_since(token_id, now - RECONCILE_S)
    classes = db.class_map(token_id)
    strong = max((r["ts"] for r in drifted if classes.get(r["address"]) == "ours"), default=None)
    weak = max((r["ts"] for r in drifted if classes.get(r["address"]) != "ours"), default=None)
    deadlines = []
    for a in candidates:
        if a not in rows:
            continue                               # never-read wallets are the delta's job
        tok, _h, obs = rows[a]
        limit = ZERO_RECHECK_S if ((not tok) and a not in fills) else RECONCILE_S
        deadline = obs + limit
        # Measured from when the drift was CAUGHT, not from the wallet's own last read -- an
        # old read would otherwise be overdue already and every such wallet read in one burst.
        if strong and obs < strong:
            deadline = min(deadline, strong + URGENT_S)
        elif weak and obs < weak:
            deadline = min(deadline, weak + URGENT_WEAK_S)
        # URGENT means "read before the movement was caught" -- judged by WHEN it was read, never by
        # which deadline happens to be earlier. Judged by deadline, 209 wallets read before a stake
        # were filed as routine because their routine deadline came first, were throttled with
        # routine work, and the correction took three hours to start.
        urgent = bool((strong and obs < strong) or (weak and obs < weak))
        deadlines.append((deadline, a, urgent))
    if not deadlines:
        return ([], []) if split else []
    deadlines.sort()
    rate = max(k / max(d - now, step_s) for k, (d, _a, _u) in enumerate(deadlines, 1))
    if split:
        # FRACTIONAL CREDIT carried between steps, so the average is the true minimum rate.
        # Rounding each step up doubled a 150-wallet token's re-reads (13/h became 24/h).
        credit = _RECONCILE_CREDIT.get(token_id, 0.0) + rate * step_s
        n = int(credit + 1e-9)
        _RECONCILE_CREDIT[token_id] = credit - n
    else:
        n = math.ceil(rate * step_s - 1e-9)
    picked = deadlines[:n]
    if not split:
        return [a for _d, a, _u in picked]
    return [a for _d, a, u in picked if u], [a for _d, a, u in picked if not u]


def _inventory(token_id: int, full: bool, window_s: int, progress, fresh: bool,
               background: bool, reconcile: float) -> TickResult:
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

    urgent_set: set[str] = set()
    routine_set: set[str] = set()
    if full:
        targets = ours + others
    else:
        rows = db.latest_balance_rows(token_id)
        known = set(rows)
        # Our wallets are NOT re-read just because they traded: their fills roll the last read
        # forward (see ledger.build). That was the cost that made a 500-wallet campaign
        # impossible -- every buy wave demanded 500 calls every five minutes. They are re-read
        # only when never read, or when a gap in the trade feed means their roll-forward may be
        # missing fills.
        gap_ts = db.latest_tape_gap_ts(token_id)
        behind_gap = {a for a in ours if gap_ts and a in rows and rows[a][2] < gap_ts}
        # Wallets a full pull could not reach are QUEUED: read before the operator's request,
        # they are picked up here as the background budget allows.
        urgent_picks, routine_picks = _reconcile_pick(token_id, ours + others, rows, reconcile,
                                                      split=True)
        if routine_picks and background and not _routine_allowed():
            # Budget is tight: the census and proven drift go first, and routine shrinks to the
            # wallets that would otherwise pass the hard limit.
            hard = db.now() - HARD_STALE_S + reconcile
            routine_picks = [a for a in routine_picks if a in rows and rows[a][2] < hard]
        urgent_set, routine_set = set(urgent_picks), set(routine_picks)
        req_ts = db.full_request_ts(token_id)
        requested = {a for a in ours + others if req_ts and a in rows and rows[a][2] < req_ts}
        # The pool moves on every fill, and a staking / locking contract moves whenever anyone
        # stakes: both are re-read every step -- a handful of addresses, and the ledger is wrong
        # about everyone's float while either is stale.
        targets = sorted((set(ours) - known) | behind_gap | requested
                         | {a for a in others if cmap[a] in ("pool", "locked")
                            and (a not in rows or db.now() - rows[a][2] >= CONTRACT_REREAD_S - 30)}
                         | (set(others) - known)
                         | set(urgent_picks) | set(routine_picks))

    skipped = [w for w in targets if not config.is_address(chain, w)]
    targets = [w for w in targets if config.is_address(chain, w)]

    # SPEND ONLY WHAT THE BUDGET ALLOWS, MOST IMPORTANT FIRST. Each of our wallets is its own
    # last read plus its own fills, so a partial sweep is no longer an inconsistent one: reading
    # the wallets the budget can afford now and the rest next hour leaves every wallet
    # individually correct. So a sweep that does not fit is not refused -- it reads what fits,
    # in priority order, and keeps a reserve so a live campaign can still price. A read the
    # cache can serve costs nothing and needs no allowance.
    latest = db.latest_balance_rows(token_id)
    gap_ts = db.latest_tape_gap_ts(token_id)

    def priority(w: str) -> tuple:
        cls = cmap.get(w)
        if cls in ("pool", "locked"):
            return (0, 0)                      # the ledger is meaningless without these
        if w not in latest:
            return (1 if cls != "ours" else 2, 0)
        if cls == "ours" and gap_ts and latest[w][2] < gap_ts:
            return (3, latest[w][2])           # its roll-forward may be missing fills
        if w in urgent_set:
            return (3, latest[w][2])           # correcting PROVEN drift comes before routine
        if w in routine_set:
            return (5, latest[w][2])           # routine last
        return (4, latest[w][2])

    targets.sort(key=priority)
    n_total = len(targets)
    deferred: list[str] = []
    # `left` is the tighter of the hour and the day FOR THIS TIER: background work stops at its
    # own share and never eats into what is kept for the operator.
    left = gmgn.budget(background=background).get("left")
    if left is not None:
        allowance = max(0, left - (0 if background else BUDGET_RESERVE))
        chosen = []
        for w in targets:
            if not fresh and gmgn.balance_cached(chain, w, address):
                chosen.append(w)
            elif allowance > 0:
                chosen.append(w)
                allowance -= 1
            else:
                deferred.append(w)
        targets = chosen
    # CONCURRENT, because the cost here is process startup, not rate limit. Sequentially this
    # is ~0.54s per wallet and the operator watches a blank desk for a minute and a half while
    # every derived figure reads zero. gmgn._pace() still enforces the global interval across
    # these threads, so the burst ceiling is unchanged.
    rows, calls, failed = [], 0, []
    first_error: list[str] = []
    stop = threading.Event()

    def _read(w: str):
        # Checked per wallet, not per sweep: a delete part-way through stops the rest rather
        # than paying for every remaining wallet on a token that is gone.
        if aborted(token_id):
            return w, None, "aborted"
        # Once the budget refuses, the rest of this sweep is deferred WITHOUT asking again --
        # asking again, once per wallet, is what wrote 27,000 refusal lines into the log.
        if stop.is_set():
            return w, None, "budget"
        try:
            # `fresh` is the operator pressing "full pull": an explicit request for current
            # numbers, so it goes past the cache. Onboarding and the background sweeps use it --
            # which is what makes deleting and re-adding a token within minutes cost nothing.
            return w, gmgn.token_balance(chain, w, address, fresh=fresh,
                                         background=background), None
        except (gmgn.BudgetExceeded, gmgn.RateLimited):
            stop.set()
            return w, None, "budget"
        except gmgn.GmgnError as exc:
            return w, None, exc

    done, skipped = 0, 0
    with ThreadPoolExecutor(max_workers=min(BALANCE_WORKERS, max(1, len(targets)))) as ex:
        for w, got, exc in ex.map(_read, targets):
            done += 1
            if exc == "aborted":
                skipped += 1
                continue
            if exc == "budget":
                deferred.append(w)
                continue
            if progress:
                progress(done, len(targets))
            if exc is not None:
                # A wallet we could not read is NOT a wallet holding nothing. It never reaches
                # record_balances, so ledger falls back to 0.0 and the operator sees an empty
                # desk. Count them and fail the tick, or the UI reports a clean sweep over
                # missing data.
                failed.append(w)
                if not first_error:
                    first_error.append(f"{w[:10]}: {str(exc)[:120]}")
            else:
                calls += 1
                rows.append((w, got[0], got[1]))
    if skipped:
        log.info("inventory for token %s aborted — %d wallet(s) never queried", token_id, skipped)
        return TickResult("inventory", False, calls, time.time() - t0,
                          f"ABORTED after {calls} of {len(targets)} wallets — {skipped} calls "
                          f"not made because the token was deleted")
    # DRIFT: a re-read that does not match the last read rolled forward by our fills means
    # tokens moved outside the trade feed -- a transfer, staking, another venue. Other wallets may
    # have moved in the same way, so it is recorded and their re-reads are brought forward. A
    # simulated week showed why it matters: transfers between our own wallets double-counted
    # 2.2% of the position while half the pair had been re-read and half had not.
    # A wallet last read before a gap in the trade feed is ALREADY scheduled for re-read, and
    # its difference is the fills the feed dropped, not a transfer. Counted as drift, one gap
    # in a buy wave set off a second full pass on top of the first.
    gap_before = db.latest_tape_gap_ts(token_id)
    prior = {w: (latest[w][1], latest[w][2]) for w, _t, _h in rows
             if w in latest and cmap.get(w) == "ours"
             and not (gap_before and latest[w][2] < gap_before)}
    rolled = db.fills_after(token_id, prior) if prior else {}
    token = db.token_row(token_id)
    supply = float(token["supply_nominal"] or 0) if token is not None else 0.0
    moved = []
    for w, tok, _h in rows:
        if w in prior:
            expected = latest[w][0] + rolled.get(w, (0.0, 0))[0]
            if abs(tok - expected) > max(1.0, DRIFT_TOLERANCE * max(abs(tok), abs(expected))):
                moved.append((w, tok - expected))
        # A STAKING OR LOCKING CONTRACT THAT JUMPS is evidence that wallets moved tokens into or
        # out of it, and ours may be among them. It is re-read every step, so this fires within
        # minutes -- whereas waiting for one of our staking wallets to come up for its own
        # re-read left a 3.4% position error standing for six hours in a simulated week.
        elif cmap.get(w) == "locked" and w in latest:
            if supply and abs(tok - latest[w][0]) > LOCKED_JUMP * supply:
                moved.append((w, tok - latest[w][0]))
    db.record_balances(token_id, rows)
    if moved:
        db.record_drift(token_id, moved)
        # %-formatting has no thousands separator: "%+,.0f" raised inside logging itself.
        net = f"{sum(d for _w, d in moved):+,.0f}"
        log.info("token %s: %d address(es) changed outside the trade feed (net %s tokens) — "
                 "re-reading our other wallets within %dh", token_id, len(moved), net,
                 URGENT_S // 3600)
    ok = not failed and not deferred
    if failed:
        # A few failures are routine and the wallets are simply read next time; many at once is
        # a pattern worth a warning.
        level = log.warning if len(failed) > max(2, len(rows) // 20) else log.info
        level("inventory for token %s: %d balance read(s) failed, e.g. %s",
              token_id, len(failed), first_error[0] if first_error else "?")
    # Deferred wallets are read by the BACKGROUND sweep, so that tier says when they will be.
    # Pause background work only when something ESSENTIAL was deferred. Routine re-reads that
    # did not fit simply wait for a later step; pausing everything for them also held back the
    # corrections the budget was supposed to protect.
    essential = [w for w in deferred if w not in routine_set]
    resume_at = (gmgn.budget(background=True, need=len(essential)).get("resumes_at")
                 if essential else None)
    status = ("ok" if ok else "partial" if deferred and rows else "deferred" if deferred
              else "failed")
    return TickResult("inventory", ok, calls, time.time() - t0, resume_at=resume_at,
                      status=status, done=len(rows), of=n_total, detail=
                      (f"{len(deferred)} of {len(targets) + len(deferred)} wallets DEFERRED — "
                       f"the hour's API budget is committed; they are read next as it frees · "
                       if deferred else "") +
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
CENSUS_SLICES = len(ORDER_BYS) + len(TAGS) * len(TAG_ORDER_BYS)


def track_census(token_id: int, background: bool = False) -> TickResult:
    """Union of many ranked slices, because each caps at 100 rows.

    COVERAGE IS STATED, NEVER ASSUMED: a wallet missing from every slice is UNCOVERED, not
    absent-and-therefore-fine. Per-slice new-address yield is logged so a slice that adds
    nothing is visible rather than presumed useful.
    """
    t0 = time.time()
    chain, address, _ = _ctx(token_id)
    need = CENSUS_SLICES
    b = gmgn.budget(background=background, need=need)
    left = b.get("left")
    if left is not None and need > left - (0 if background else BUDGET_RESERVE):
        return TickResult("census", False, 0, time.time() - t0,
                          f"DEFERRED — a census needs {need} calls and {left} are available",
                          resume_at=b.get("resumes_at"), status="deferred")

    def interrupted(exc: Exception) -> TickResult:
        # A census the budget cuts short is DISCARDED, not recorded. Recorded, it became the
        # latest census -- 46 holders where the last complete one had 132 -- and everything
        # built on holders silently used the partial one. The previous complete census stays.
        log.info("census for token %s interrupted after %d slices (%s) — kept the previous "
                 "complete census", token_id, slices, str(exc)[:80])
        return TickResult("census", False, calls, time.time() - t0,
                          f"INCOMPLETE after {slices} slices — budget; kept the previous census",
                          resume_at=gmgn.budget(background=background,
                                                need=CENSUS_SLICES).get("resumes_at"),
                          status="deferred")
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
            n = absorb(gmgn.traders(chain, address, order_by=ob, background=background), None)
            calls += 1
            slices += 1
            log.debug("census order-by %s: +%d new (union %d)", ob, n, len(rows))
        except (gmgn.BudgetExceeded, gmgn.RateLimited) as exc:
            return interrupted(exc)
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
                absorb(gmgn.traders(chain, address, order_by=ob, tag=tg,
                                    background=background), tg)
                calls += 1
                slices += 1
            except (gmgn.BudgetExceeded, gmgn.RateLimited) as exc:
                return interrupted(exc)
            except gmgn.GmgnError:
                pass

    if not rows:
        return TickResult("census", False, calls, time.time() - t0, "no rows returned")

    sweep = db.now()
    info = {}
    try:
        info = gmgn.token_info(chain, address, background=background)
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
