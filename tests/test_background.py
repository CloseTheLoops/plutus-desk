"""Background work must never exhaust the GMGN budget, and must stay quiet while it waits.

WHAT HAPPENED. With nobody using the desk, the tracker loop spent 9,999 of a 10,000 daily budget,
pinned it there from 01:00, and wrote 27,000 log lines. The causes, each pinned below:

  * a full re-read of every wallet was due whenever the stalest wallet was over an hour old --
    and a budget-limited read never reached the stalest, so it was due again every five minutes;
  * the sweep's allowance looked only at the hourly budget, so once the DAILY cap was spent every
    wallet read was refused, and each refusal was logged;
  * background work and the operator shared one ceiling, so background could take all of it;
  * a 429 paused only the thread that received it, and the CLI fallback repeated the request;
  * the "scan in progress" guard lived in one process's memory, invisible to the CLI;
  * a census the budget cut short was recorded as the latest -- 46 holders where the last
    complete one found 132.

Target: under ~150 GMGN calls an hour in steady state with 458 wallets.
"""
from __future__ import annotations

import os as _os_guard
_os_guard.environ.setdefault("PLUTUS_ETHERSCAN_DISABLE", "1")   # never real Etherscan here
_os_guard.environ.setdefault("PLUTUS_RPC_DISABLE", "1")          # never a real chain node here

import importlib
import logging
import math
import os
import pathlib
import sys
import tempfile
import threading
import time
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plutus import config  # noqa: E402

TOKEN = "0x" + "9" * 40
POOL = "0x" + "c" * 40
_RUN = [0]


def _new_path() -> str:
    _RUN[0] += 1
    return str(pathlib.Path(tempfile.gettempdir())
               / f"plutus_bg_{_RUN[0]}_{time.time_ns()}.db")


def _close_all():
    from plutus.sources import gmgn
    try:
        from plutus import db as _prev
        if getattr(_prev, "_conn", None) is not None:
            _prev._conn.close()
            _prev._conn = None
    except ImportError:
        pass
    if getattr(gmgn, "_pace_conn", None) is not None:
        gmgn._pace_conn.close()
        gmgn._pace_conn = None


def _real(hour=20, day=1000, bg_hour="0.5", bg_day="0.6"):
    """The real gmgn module against a fresh database, with the given caps."""
    _close_all()
    os.environ.update(PLUTUS_MAX_CALLS_HOUR=str(hour), PLUTUS_MAX_CALLS_DAY=str(day),
                      PLUTUS_BG_HOUR_SHARE=bg_hour, PLUTUS_BG_DAY_SHARE=bg_day)
    config.DB_PATH = _new_path()
    from plutus.sources import gmgn
    importlib.reload(gmgn)
    return gmgn


def _fresh_db(n_ours=0):
    """Real gmgn module restored, a fresh database, and a FAKE CLOCK for db.now()."""
    gmgn = _real(hour=900, day=10000)
    from plutus import db
    importlib.reload(db)
    clock = [1_800_000_000.0]
    db.now = lambda: int(clock[0])
    tid = db.upsert_token("robinhood", TOKEN, symbol="S", supply_nominal=1_000_000_000,
                          primary_venue="0x" + "e" * 64)
    wallets = ["0x%040x" % (i + 1) for i in range(n_ours)]
    for w in wallets:
        db.classify(tid, w, "ours", source="operator")
    db.classify(tid, POOL, "pool", source="operator")
    from plutus.track import trackers as T
    T.clear_abort(tid)
    return db, gmgn, T, tid, wallets, clock


class Vendor:
    """Stands in for GMGN: every call is timestamped on the fake clock."""

    def __init__(self, gmgn, clock, fail_after=None):
        self.gmgn, self.clock, self.fail_after, self.stamps = gmgn, clock, fail_after, []
        gmgn.token_balance = self.balance
        gmgn.token_pool = self.pool
        gmgn.traders = self.traders
        gmgn.token_info = self.info
        gmgn.budget = self.budget
        gmgn.balance_cached = lambda c, w, t: False

    def _spend(self):
        if self.fail_after is not None and len(self.stamps) >= self.fail_after:
            raise self.gmgn.BudgetExceeded("background daily GMGN budget spent")
        self.stamps.append(self.clock[0])

    def balance(self, chain, w, token, fresh=False, background=False):
        self._spend()
        return 1000.0, 100

    def pool(self, chain, address, fresh=False, background=False):
        self._spend()
        return {"base_reserve": 140_000_000.0, "quote_reserve": 11_700.0, "liquidity": 23_400.0}

    def traders(self, chain, address, order_by="amount_percentage", tag=None, limit=100,
                attempts=2, background=False):
        self._spend()
        return [{"address": "0x%040x" % (90_000 + i), "balance": 1.0} for i in range(3)]

    def info(self, chain, address, background=False):
        self._spend()
        return {"holder_count": 132}

    def budget(self, background=False, need=1):
        return {"left": 10 ** 9, "hour_left": 10 ** 9, "day_left": 10 ** 9, "ok": True,
                "resumes_at": self.clock[0] + 60}


def _trusted_free_pool():
    from plutus.sources import gecko
    gecko.pool = lambda net, pid: {"base_reserve": 140_000_000.0, "quote_reserve": 11_700.0,
                                   "spot": 11_700.0 / 140_000_000.0, "reserve_usd": 23_400.0}


# ── 2. background has its own ceiling; the operator keeps the rest ────────────────
def test_background_stops_at_its_share_while_the_operator_keeps_going():
    gm = _real(hour=20, day=1000, bg_hour="0.5")
    bg = 0
    try:
        while True:
            gm._reserve_slot(background=True)
            bg += 1
    except gm.BudgetExceeded:
        pass
    assert bg == 10, f"background should stop at 50% of 20, stopped at {bg}"
    op = 0
    try:
        while True:
            gm._reserve_slot()
            op += 1
    except gm.BudgetExceeded:
        pass
    assert op == 10, f"the operator should still get the other 10 calls, got {op}"


def test_the_daily_share_binds_background_too():
    gm = _real(hour=1000, day=20, bg_day="0.6")
    n = 0
    try:
        while True:
            gm._reserve_slot(background=True)
            n += 1
    except gm.BudgetExceeded:
        pass
    assert n == 12, f"background should stop at 60% of a 20-call day, stopped at {n}"


# ── 3. left = the TIGHTER of hour and day, and when slots free ────────────────────
def test_left_is_the_tighter_of_the_hour_and_the_day():
    """THE bug: a sweep sized by the hour kept going after the day was spent."""
    gm = _real(hour=1000, day=10, bg_day="1.0")
    for _ in range(10):
        gm._reserve_slot(background=True)
    b = gm.budget(background=True)
    assert b["hour_left"] > 0 and b["day_left"] == 0
    assert b["left"] == 0, f"left={b['left']} while the day is spent — the sweep would keep going"


def test_resumes_at_is_when_enough_calls_age_out():
    gm = _real(hour=10, day=1000, bg_hour="1.0")
    first = time.time()
    for _ in range(10):
        gm._reserve_slot(background=True)
    r = gm.budget(background=True, need=1)["resumes_at"]
    assert first + 3590 < r < first + 3700, \
        f"one slot frees when the oldest call leaves the hour, ~{first + 3600:.0f}; got {r:.0f}"


# ── 6. a 429 pauses everyone, once, and is never repeated on the CLI ───────────────
def test_a_429_pauses_every_caller_including_other_threads():
    gm = _real(hour=1000, day=10000)
    gm.pause_all(1.5, "test")
    got = []
    t = threading.Thread(target=lambda: got.append(gm._reserve_slot()))
    t.start()
    t.join()
    assert 1.2 < got[0] < 1.8, f"another thread was not held by the pause: waited {got[0]:.2f}s"


def test_a_long_pause_is_not_cancelled_by_the_skew_guard():
    """The guard used to treat anything >5s ahead as a broken clock and cancel it."""
    gm = _real(hour=1000, day=10000)
    gm.pause_all(60, "test")
    wait = gm._reserve_slot()
    assert wait > 50, f"a deliberate 60s pause was cancelled: waited {wait:.1f}s"


def test_a_429_is_never_repeated_on_the_cli():
    gm = _real(hour=1000, day=10000)

    def limited(args, **k):
        raise gm.RateLimited("RATE_LIMIT test")

    gm._http_call = limited
    ran = []
    orig = gm.subprocess.run
    gm.subprocess.run = lambda *a, **k: ran.append(a)
    try:
        try:
            gm.call("token", "info", "--chain", "robinhood", "--address", TOKEN, attempts=2,
                    fresh=True)
            raise AssertionError("a rate limit was swallowed")
        except gm.RateLimited:
            pass
    finally:
        gm.subprocess.run = orig
    assert not ran, "after a 429 the same request was sent again through the CLI"


# ── 3. a spent budget stops the sweep quietly ──────────────────────────────────────
def test_a_spent_budget_ends_the_sweep_without_a_line_per_wallet():
    db, gmgn, T, tid, wallets, clock = _fresh_db(n_ours=200)
    Vendor(gmgn, clock, fail_after=5)
    seen = []
    h = logging.Handler()
    h.emit = seen.append
    T.log.addHandler(h)
    try:
        r = T.track_inventory(tid, full=True, background=True)
    finally:
        T.log.removeHandler(h)
    warnings = [x for x in seen if x.levelno >= logging.WARNING]
    assert len(warnings) <= 1, f"{len(warnings)} warnings for one spent budget — should be one or none"
    assert "DEFERRED" in r.detail and r.resume_at, r.detail


# ── 1 + 4. rolling reconciliation, and idle zero wallets wait a day ─────────────────
def test_one_step_reads_a_few_not_the_whole_set():
    db, gmgn, T, tid, wallets, clock = _fresh_db(n_ours=458)
    v = Vendor(gmgn, clock)
    db.record_balances(tid, [(w, 1000.0, 100) for w in wallets + [POOL]])
    clock[0] += 3600
    T.track_inventory(tid, background=True, reconcile=300)
    assert 1 < len(v.stamps) <= 1 + 8, (
        f"one step read {len(v.stamps)} wallets — it should read the pool and a handful")


def test_every_deadline_is_met_at_the_minimum_rate_even_after_a_cohort():
    """The 15-hour wallets: every wallet read in the same hour must not all come due together."""
    db, gmgn, T, tid, wallets, clock = _fresh_db(n_ours=458)
    v = Vendor(gmgn, clock)
    db.record_balances(tid, [(w, 1000.0, 100) for w in wallets + [POOL]])   # one cohort
    worst = 0.0
    steps = 36 * 12                                                         # 36h of 5-min steps
    for _ in range(steps):
        clock[0] += 300
        T.track_inventory(tid, background=True, reconcile=300)
        rows = db.latest_balance_rows(tid)
        worst = max(worst, max(clock[0] - rows[w][2] for w in wallets))
    wallet_reads = len(v.stamps) - steps                                    # minus the pool
    assert worst <= T.RECONCILE_S + 300, (
        f"a wallet reached {worst / 3600:.1f}h without a read (limit {T.RECONCILE_S / 3600:.0f}h)")
    minimum = 458 * 36 / (T.RECONCILE_S / 3600)
    assert wallet_reads <= minimum * 1.35, (
        f"{wallet_reads} wallet reads in 36h against a minimum of ~{minimum:.0f}")


def test_idle_zero_wallets_are_re_read_at_most_daily():
    db, gmgn, T, tid, wallets, clock = _fresh_db(n_ours=0)
    zero, held, traded = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40
    for w in (zero, held, traded):
        db.classify(tid, w, "ours", source="operator")
    db.record_balances(tid, [(zero, 0.0, 100), (held, 5.0, 100), (traded, 0.0, 100)])
    db.record_trades([(tid, "0xf1", 0, "p", int(clock[0]) + 60, "buy", 1.0, 3.0, 0.1, traded, 1,
                       "t", 150)])
    rows = db.latest_balance_rows(tid)
    clock[0] += 13 * 3600
    got = set(T._reconcile_pick(tid, [zero, held, traded], rows, 300))
    assert zero not in got, "an idle zero wallet was re-read after 13h"
    assert {held, traded} <= got, "a funded wallet, or a zero wallet that has traded, was skipped"
    clock[0] += 12 * 3600
    assert zero in set(T._reconcile_pick(tid, [zero, held, traded], rows, 300)), \
        "an idle zero wallet was never re-read after a day"


# ── 7. one full read at a time, across processes ───────────────────────────────────
def test_the_scan_lock_is_exclusive_and_lapses():
    db, gmgn, T, tid, wallets, clock = _fresh_db()
    assert db.acquire_scan_lock(tid, "server:1:a", ttl=100) is None
    held = db.acquire_scan_lock(tid, "cli:2:b", ttl=100)
    assert held and held["owner"] == "server:1:a", "a second owner took a held lock"
    clock[0] += 101
    assert db.acquire_scan_lock(tid, "cli:2:b", ttl=100) is None, "an expired lock blocked forever"
    db.release_scan_lock(tid, "cli:2:b")
    assert db.scan_lock_holder(tid) is None


def test_a_full_read_refuses_while_another_process_holds_the_lock():
    db, gmgn, T, tid, wallets, clock = _fresh_db(n_ours=10)
    v = Vendor(gmgn, clock)
    db.acquire_scan_lock(tid, "otherhost:999:x")
    r = T.track_inventory(tid, full=True)
    assert "SKIPPED" in r.detail and not v.stamps, \
        f"a full read ran while another process held the lock ({len(v.stamps)} calls)"


# ── 5. the census comes first, and a partial one is never mistaken for complete ─────
def test_an_interrupted_census_keeps_the_previous_complete_one():
    db, gmgn, T, tid, wallets, clock = _fresh_db()
    db.connect().execute("INSERT INTO census_meta VALUES (?,?,?,?,?,?,?)",
                         (tid, int(clock[0]) - 3600, T.CENSUS_SLICES, 132, 132, 36, 5.0))
    db.connect().commit()
    Vendor(gmgn, clock, fail_after=5)
    r = T.track_census(tid, background=True)
    assert not r.ok and "INCOMPLETE" in r.detail and r.resume_at, r.detail
    assert db.latest_census_meta(tid)["rows_found"] == 132, \
        "a census the budget cut short replaced the complete one"


def test_a_partial_census_is_flagged_where_advice_is_built():
    db, gmgn, T, tid, wallets, clock = _fresh_db()
    now = int(clock[0])
    for ts, found in ((now - 4000, 132), (now - 100, 46)):
        db.connect().execute("INSERT INTO census_meta VALUES (?,?,?,?,?,?,?)",
                             (tid, ts, T.CENSUS_SLICES, found, 132, 36, 5.0))
    db.connect().commit()
    from plutus.analyze import ledger as L
    importlib.reload(L)
    notes = L.census_notes(tid)
    assert any(n.startswith("CENSUS PARTIAL") for n in notes), notes
    assert any(n.startswith("census: 46 of 132") for n in notes), "age/coverage not surfaced"
    import inspect
    from plutus.analyze import campaign
    assert "census_notes" in inspect.getsource(campaign.status), \
        "campaign advice does not carry the partial-census warning"


# ── the target ──────────────────────────────────────────────────────────────────────
def test_steady_state_is_under_150_calls_an_hour_at_458_wallets():
    """458 wallets, onboarded once, then 36 simulated hours of background work with no users."""
    db, gmgn, T, tid, wallets, clock = _fresh_db(n_ours=458)
    v = Vendor(gmgn, clock)
    _trusted_free_pool()
    T.track_inventory(tid, full=True)                     # onboarding: the operator's read
    T.track_pool(tid)
    t0, onboard = clock[0], len(v.stamps)

    last_census = -1e18
    for step in range(36 * 12):                           # the loop's 5-minute cadence
        clock[0] = t0 + (step + 1) * 300
        T.track_pool(tid, background=True)
        if clock[0] - last_census >= 3600:
            T.track_census(tid, background=True)
            T.track_pool(tid, verify=True, background=True)
            last_census = clock[0]
        T.track_inventory(tid, background=True, reconcile=300)

    hours = Counter(int((s - t0) // 3600) for s in v.stamps[onboard:])
    steady = [hours.get(h, 0) for h in range(13, 36)]
    rows = db.latest_balance_rows(tid)
    oldest = max(clock[0] - obs for a, (_t, _h, obs) in rows.items() if a in wallets)
    print(f"      onboarding {onboard} calls · steady state {min(steady)}–{max(steady)} calls/hour "
          f"(target < 150) · stalest wallet {oldest / 3600:.1f}h old")
    assert max(steady) < 150, f"steady state reached {max(steady)} calls in an hour"
    assert oldest <= T.RECONCILE_S, \
        f"a wallet went {oldest / 3600:.1f}h without a real read (limit {T.RECONCILE_S / 3600:.0f}h)"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                fails += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
