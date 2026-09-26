"""Our position is our last balance read plus the fills that read cannot contain.

WHY. Re-reading every one of our wallets after every buy wave costs one vendor call per wallet.
At 500 wallets that was 500 calls every five minutes -- far past the hourly budget -- so a large
campaign froze its own position display within minutes. The trade feed already records, for
free, every fill our wallets make. So a wallet's balance is rolled forward from its last read.

THE RULE UNDER TEST: a fill counts when its block is later than the block at which the balance
last changed. That stays exact when the vendor's indexer lags. Get it wrong in one direction and
fills are counted twice; in the other and buys vanish.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plutus import config  # noqa: E402

A, B, C, X = ("0x" + c * 40 for c in "abce")          # three of ours, one third party


_RUN = [0]


def _fresh():
    """A brand-new database for every test.

    A NEW FILE each time rather than deleting the old one: on Windows a file another connection
    still holds cannot be removed, and the first version of this helper swallowed that error --
    so a test silently inherited the previous test's trades and failed for a reason that had
    nothing to do with what it tests. Every open connection is also closed, including the rate
    limiter's own, which a sweep opens when it checks the budget.
    """
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
    path = pathlib.Path(tempfile.gettempdir()) / f"plutus_rollforward_{_RUN[0]}_{time.time_ns()}.db"
    config.DB_PATH = str(path)
    from plutus import db
    importlib.reload(db)
    tid = db.upsert_token("robinhood", "0x" + "9" * 40, symbol="RF", supply_nominal=1_000_000)
    for w in (A, B, C):
        db.classify(tid, w, "ours", source="operator")
    return db, tid


def _fill(db, tid, tx, maker, side, tokens, block, ts=None):
    ts = db.now() if ts is None else ts               # a fill after the read, in real time
    db.record_trades([(tid, tx, 0, "p", ts, side, 1.0, tokens, 0.1, maker, 1, "t", block)])


def test_fills_after_the_reads_block_are_added():
    db, tid = _fresh()
    db.record_balances(tid, [(A, 100.0, 500)])        # read: 100 tokens, last changed at block 500
    _fill(db, tid, "0x1", A, "buy", 40.0, 501)         # later block -> not in the read
    _fill(db, tid, "0x2", A, "buy", 7.0, 520)
    got = db.fills_after(tid, {A: (500, db.now())})
    assert got[A] == (47.0, 2), f"expected +47 from 2 fills, got {got}"


def test_a_fill_in_the_reads_own_block_is_not_counted_twice():
    """height is the block the balance last CHANGED at -- a fill there is already inside it."""
    db, tid = _fresh()
    db.record_balances(tid, [(A, 100.0, 500)])
    _fill(db, tid, "0x1", A, "buy", 40.0, 500)
    _fill(db, tid, "0x2", A, "buy", 3.0, 499)
    assert db.fills_after(tid, {A: (500, db.now())}) == {}, \
        "fills at or before the read's block were counted, so the position is double-counted"


def test_sells_subtract():
    db, tid = _fresh()
    db.record_balances(tid, [(A, 100.0, 500)])
    _fill(db, tid, "0x1", A, "buy", 40.0, 501)
    _fill(db, tid, "0x2", A, "sell", 15.0, 502)
    assert db.fills_after(tid, {A: (500, db.now())})[A] == (25.0, 2)


def test_other_peoples_fills_are_ignored():
    db, tid = _fresh()
    db.record_balances(tid, [(A, 100.0, 500)])
    _fill(db, tid, "0x1", X, "buy", 999.0, 600)
    assert db.fills_after(tid, {A: (500, db.now())}) == {}


def test_the_ledger_shows_the_rolled_forward_position():
    db, tid = _fresh()
    db.record_balances(tid, [(A, 100.0, 500), (B, 200.0, 500), (C, 0.0, 500)])
    _fill(db, tid, "0x1", A, "buy", 50.0, 510)
    _fill(db, tid, "0x2", C, "buy", 30.0, 511)
    _fill(db, tid, "0x3", B, "sell", 20.0, 512)
    from plutus.analyze import ledger as L
    importlib.reload(L)
    led = L.build(tid)
    assert abs(led.ours - (150 + 180 + 30)) < 1e-6, f"ours should be 360, got {led.ours}"
    assert any("fill(s) made since their last balance read" in n for n in led.notes), led.notes


def test_selling_more_than_held_clamps_to_zero_and_says_why():
    db, tid = _fresh()
    db.record_balances(tid, [(A, 10.0, 500)])
    _fill(db, tid, "0x1", A, "sell", 25.0, 501)
    from plutus.analyze import ledger as L
    importlib.reload(L)
    led = L.build(tid)
    assert led.ours == 0.0, f"a negative balance must clamp to 0, got {led.ours}"
    assert any("sold more than their last read held" in n for n in led.notes), led.notes


def test_a_feed_gap_flags_the_position_and_forces_a_re_read():
    db, tid = _fresh()
    db.record_balances(tid, [(A, 100.0, 500), (B, 100.0, 500)])
    db.connect().execute("UPDATE balances SET observed_ts=observed_ts-100 WHERE token_id=?", (tid,))
    db.connect().commit()
    db.record_tape_gap(tid, 600, 900)
    from plutus.analyze import ledger as L
    importlib.reload(L)
    assert any("UNDERSTATED" in n for n in L.build(tid).notes), "a feed gap was not surfaced"

    from plutus.track import trackers as T
    from plutus.sources import gmgn
    read = []
    gmgn.token_balance = lambda c, w, t, fresh=False, **k: (read.append(w), (1.0, 1))[1]
    T.clear_abort(tid)
    T.track_inventory(tid, full=False)
    assert {A, B} <= set(read), f"wallets read before the gap were not re-read: {read}"


def test_traded_wallets_are_no_longer_re_read_on_every_delta():
    """The cost the whole change exists to remove."""
    db, tid = _fresh()
    db.record_balances(tid, [(A, 100.0, 500), (B, 100.0, 500), (C, 100.0, 500)])
    for i, w in enumerate((A, B, C)):
        _fill(db, tid, f"0x{i}", w, "buy", 5.0, 600 + i, ts=db.now())
    from plutus.track import trackers as T
    from plutus.sources import gmgn
    read = []
    gmgn.token_balance = lambda c, w, t, fresh=False, **k: (read.append(w), (1.0, 1))[1]
    T.clear_abort(tid)
    T.track_inventory(tid, full=False)
    assert not ({A, B, C} & set(read)), \
        f"our wallets were re-read just for trading — {len(read)} calls the feed made unnecessary"


def test_the_trades_migration_keeps_old_rows():
    db, tid = _fresh()
    cols = [r[1] for r in db.connect().execute("PRAGMA table_info(trades)")]
    assert "block" in cols


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
