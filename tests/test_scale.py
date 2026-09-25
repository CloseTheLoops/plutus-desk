"""A 500-wallet campaign must fit inside the hourly GMGN budget, and stay live while it runs.

WHY. Walking a 500-wallet, $10k campaign through the code found three separate ways it ran out
of budget -- onboarding read every wallet twice (~1,050 calls against a 900-an-hour cap before a
campaign began), every buy wave re-read every wallet that traded (500 calls every five minutes),
and pool prices came from the same budget, so once it was spent the campaign priced its advice
off stale reserves. These tests pin each fix, then run the scenario and count the calls.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plutus import config  # noqa: E402

TOKEN = "0x" + "9" * 40
_RUN = [0]


def _fresh(n_ours=0):
    """A new database file per test, every connection closed first (see test_rollforward)."""
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
    _RUN[0] += 1
    config.DB_PATH = str(pathlib.Path(tempfile.gettempdir())
                         / f"plutus_scale_{_RUN[0]}_{time.time_ns()}.db")
    from plutus import db
    importlib.reload(db)
    tid = db.upsert_token("robinhood", TOKEN, symbol="S", supply_nominal=1_000_000_000,
                          primary_venue="0x" + "e" * 64)
    wallets = ["0x%040x" % (i + 1) for i in range(n_ours)]
    for w in wallets:
        db.classify(tid, w, "ours", source="operator")
    db.classify(tid, "0x" + "c" * 40, "pool", source="operator")
    return db, tid, wallets


class _Vendor:
    """Stands in for GMGN: counts every call and enforces a real hourly budget."""

    def __init__(self, gmgn, cap=900):
        self.calls, self.cap, self.gmgn = 0, cap, gmgn
        gmgn.token_balance = self.balance
        gmgn.token_pool = self.pool
        gmgn.budget = self.budget
        gmgn.balance_cached = lambda c, w, t: False

    def _spend(self):
        if self.calls >= self.cap:
            raise self.gmgn.BudgetExceeded("cap")
        self.calls += 1

    def balance(self, chain, w, token, fresh=False):
        self._spend()
        return 1000.0, 100

    def pool(self, chain, address, fresh=False):
        self._spend()
        return {"base_reserve": 140_000_000.0, "quote_reserve": 11_700.0, "liquidity": 23_400.0}

    def budget(self):
        return {"hour_left": self.cap - self.calls, "ok": self.calls < self.cap}


def _free_pool(agree=True):
    from plutus.sources import gecko
    r = 140_000_000.0 if agree else 350_000_000.0
    gecko.pool = lambda net, pid: {"base_reserve": r, "quote_reserve": 11_700.0,
                                   "spot": 11_700.0 / r, "reserve_usd": 23_400.0}


# ── #2: the free pool source, gated by a measured check ──────────────────────────
def test_a_pool_that_agrees_is_read_free_after_one_check():
    db, tid, _ = _fresh()
    from plutus.sources import gmgn
    from plutus.track import trackers as T
    v = _Vendor(gmgn)
    _free_pool(agree=True)
    T.clear_abort(tid)
    T.track_pool(tid)                          # first use: checks against GMGN once
    assert v.calls == 1, f"the first read should check against GMGN once, made {v.calls}"
    for _ in range(20):
        T.track_pool(tid)
    assert v.calls == 1, f"a trusted free source still cost {v.calls - 1} GMGN calls"
    assert db.latest_pool(tid)["source"] == "gecko"


def test_a_pool_that_disagrees_stays_on_gmgn():
    """The concentrated-liquidity case: the free source's reserves were 150-240% wrong."""
    db, tid, _ = _fresh()
    from plutus.sources import gmgn
    from plutus.track import trackers as T
    _Vendor(gmgn)
    _free_pool(agree=False)
    T.clear_abort(tid)
    T.track_pool(tid)
    assert db.latest_pool(tid)["source"] == "gmgn", "a pool that failed the check used the free source"
    assert not db.latest_pool_parity(tid)["ok"]


def test_a_spent_budget_does_not_take_a_trusted_pool_offline():
    """The whole point: prices stay live while a wallet sweep spends the budget."""
    db, tid, _ = _fresh()
    from plutus.sources import gmgn
    from plutus.track import trackers as T
    v = _Vendor(gmgn)
    _free_pool(agree=True)
    T.clear_abort(tid)
    T.track_pool(tid)
    v.calls = v.cap                            # the hour's budget is gone
    r = T.track_pool(tid, verify=True)         # even the hourly re-check cannot run
    assert r.ok, f"a trusted pool went offline when the budget ran out: {r.detail}"
    assert db.latest_pool(tid)["source"] == "gecko"


# ── #1: never read a wallet twice, never start what the budget cannot pay for ─────
def test_a_sweep_reads_what_the_budget_allows_most_important_first():
    db, tid, wallets = _fresh(n_ours=100)
    from plutus.sources import gmgn
    from plutus.track import trackers as T
    v = _Vendor(gmgn, cap=T.BUDGET_RESERVE + 30)   # room for 30 reads after the reserve
    read = []
    v.balance = lambda c, w, t, fresh=False: (read.append(w), v._spend(), (1.0, 1))[2]
    gmgn.token_balance = v.balance
    T.clear_abort(tid)
    r = T.track_inventory(tid, full=True)
    assert len(read) == 30, f"expected exactly the 30 affordable reads, made {len(read)}"
    assert read[0] == "0x" + "c" * 40, "the pool was not read first"
    assert "DEFERRED" in r.detail, r.detail
    assert v.calls <= v.cap - T.BUDGET_RESERVE, "the sweep ate into the reserve kept for pricing"


def test_cached_reads_cost_no_allowance():
    db, tid, wallets = _fresh(n_ours=40)
    from plutus.sources import gmgn
    from plutus.track import trackers as T
    v = _Vendor(gmgn, cap=T.BUDGET_RESERVE + 5)
    gmgn.balance_cached = lambda c, w, t: True     # everything answerable from cache
    T.clear_abort(tid)
    r = T.track_inventory(tid, full=True)
    assert "DEFERRED" not in r.detail, "cached reads were counted against the budget"


def test_an_explicit_full_pull_bypasses_the_cache():
    db, tid, wallets = _fresh(n_ours=5)
    from plutus.sources import gmgn
    from plutus.track import trackers as T
    v = _Vendor(gmgn)
    gmgn.balance_cached = lambda c, w, t: True
    seen = []
    gmgn.token_balance = lambda c, w, t, fresh=False: (seen.append(fresh), (1.0, 1))[1]
    T.clear_abort(tid)
    T.track_inventory(tid, full=True, fresh=True)
    assert seen and all(seen), "a full pull the operator asked for was served from cache"


def test_the_loop_does_not_start_a_second_full_read_during_onboarding():
    import asyncio
    db, tid, _ = _fresh(n_ours=3)
    from plutus.web import app

    async def go():
        async def slow():
            await asyncio.sleep(5)
        app._scans.clear()
        app._scans[tid] = asyncio.create_task(slow())
        await asyncio.sleep(0)
        busy = app._inventory_busy(tid)
        app._scans[tid].cancel()
        return busy

    assert asyncio.run(go()), "the loop would start a full read while onboarding's is running"


def test_a_wallet_that_never_reads_does_not_make_every_cycle_a_full_sweep():
    """stalest_balance_ts must ignore never-read wallets, or one failing wallet re-reads all."""
    db, tid, wallets = _fresh(n_ours=3)
    db.record_balances(tid, [(wallets[0], 1.0, 1), (wallets[1], 1.0, 1)])  # wallets[2] never read
    assert db.stalest_balance_ts(tid) is not None, \
        "one unread wallet made 'stalest' None, which makes a full re-read due every cycle"


def test_census_defers_rather_than_starting_what_it_cannot_finish():
    db, tid, _ = _fresh()
    from plutus.sources import gmgn
    from plutus.track import trackers as T
    _Vendor(gmgn, cap=T.BUDGET_RESERVE + 10)
    T.clear_abort(tid)
    r = T.track_census(tid)
    assert not r.ok and "DEFERRED" in r.detail and r.calls == 0, r.detail


# ── the scenario, counted ────────────────────────────────────────────────────────
def test_a_500_wallet_campaign_fits_the_hourly_budget():
    """Onboard 500 wallets, buy with all of them, then run an hour of background sweeps."""
    db, tid, wallets = _fresh(n_ours=500)
    from plutus.sources import gmgn
    from plutus.track import trackers as T
    v = _Vendor(gmgn)
    _free_pool(agree=True)
    T.clear_abort(tid)

    T.track_inventory(tid, full=True)                  # onboarding's scan
    T.track_pool(tid)                                  # the loop's first cycle: pool check
    setup = v.calls                                    # (it no longer re-reads the wallets)

    for i, w in enumerate(wallets):                    # the buy wave, seen on the free feed
        db.record_trades([(tid, f"0x{i:064x}", 0, "p", db.now(), "buy", 20.0, 1000.0, 0.02,
                           w, 1, "t", 200 + i)])
    before_wave = v.calls
    for _ in range(12):                                # an hour of 5-minute sweeps
        T.track_inventory(tid, full=False)
        T.track_pool(tid)
    during = v.calls - before_wave

    from plutus.analyze import ledger as L
    importlib.reload(L)
    led = L.build(tid)
    print(f"      setup {setup} calls (was ~1,050) · an hour after the buy {during} calls "
          f"(was ~6,000) · position {led.ours:,.0f} tokens across 500 wallets")
    assert setup <= 510, f"setup cost {setup} calls — wallets are being read more than once"
    assert during <= 20, f"an hour of sweeps after the buy cost {during} calls"
    assert abs(led.ours - 500 * 2000.0) < 1e-6, \
        f"the position did not include the buy wave: {led.ours:,.0f}"


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
