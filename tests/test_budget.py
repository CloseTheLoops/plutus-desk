"""Three guards against spending an account's quota. Each exists because of a real suspension.

WHAT WENT WRONG. Pacing caps calls per SECOND and says nothing about calls per hour. Roughly
1,000 calls over ninety minutes never tripped the pacer once, and that sustained pattern is
exactly what vendor abuse detection watches. Three separate holes fed it:

  * a deleted token's scans kept running. Cancelling an asyncio task does not interrupt the
    thread under `asyncio.to_thread`, so a 155-wallet sweep for a token that no longer existed
    still made all 155 calls.
  * nothing capped total volume, so normal use could spend the whole quota.
  * delete + re-onboard -- the correct way to get a clean slate -- re-asked the vendor every
    question it had answered a minute earlier.

Delete still means gone. The cache is keyed by the CALL, which contains the token address, and
holds no token_id, so a delete does not touch it: re-adding builds a genuinely fresh token out
of answers already paid for.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _fresh(hour="25", day="100"):
    dbp = pathlib.Path(tempfile.gettempdir()) / f"plutus_budget_{os.getpid()}.db"
    # Close the pacer's connection from a previous case, or Windows refuses to unlink the file.
    try:
        from plutus.sources import gmgn as _prev
        if getattr(_prev, "_pace_conn", None) is not None:
            _prev._pace_conn.close()
            _prev._pace_conn = None
    except ImportError:
        pass
    for s in ("", "-wal", "-shm"):
        try:
            pathlib.Path(str(dbp) + s).unlink(missing_ok=True)
        except OSError:
            pass
    os.environ["PLUTUS_MAX_CALLS_HOUR"] = hour
    os.environ["PLUTUS_MAX_CALLS_DAY"] = day
    from plutus import config
    config.DB_PATH = str(dbp)
    from plutus.sources import gmgn
    importlib.reload(gmgn)
    return gmgn, dbp


def test_the_budget_refuses_rather_than_spending_through_the_cap():
    gmgn, _ = _fresh(hour="25")
    made = 0
    try:
        for _ in range(60):
            gmgn._pace()
            made += 1
    except gmgn.BudgetExceeded:
        pass
    assert made == 25, f"expected to stop at exactly the 25-call cap, made {made}"
    assert not gmgn.budget()["ok"]


def test_the_budget_is_shared_across_processes():
    """Two processes must not each get their own quota — that is how it was spent twice over."""
    gmgn, dbp = _fresh(hour="6")
    for _ in range(6):
        try:
            gmgn._pace()
        except gmgn.BudgetExceeded:
            break
    script = textwrap.dedent("""
        import sys, os
        sys.path.insert(0, %r)
        os.environ['PLUTUS_MAX_CALLS_HOUR'] = '6'
        from plutus import config; config.DB_PATH = %r
        from plutus.sources import gmgn
        try:
            gmgn._pace(); print('MADE_A_CALL')
        except gmgn.BudgetExceeded: print('REFUSED')
    """) % (str(ROOT), str(dbp))
    f = pathlib.Path(tempfile.gettempdir()) / "plutus_budget_worker.py"
    f.write_text(script, encoding="utf-8")
    out = subprocess.run([sys.executable, str(f)], capture_output=True, text=True,
                         timeout=60).stdout
    f.unlink(missing_ok=True)
    assert "REFUSED" in out, \
        "a second process was granted its own budget — the cap is per-process, not per-key"


def test_a_deleted_token_stops_its_sweep_mid_flight():
    """Cancellation cannot reach a thread; the abort flag has to."""
    dbp = pathlib.Path(tempfile.gettempdir()) / "plutus_abort_test.db"
    for s in ("", "-wal", "-shm"):
        pathlib.Path(str(dbp) + s).unlink(missing_ok=True)
    from plutus import config
    config.DB_PATH = str(dbp)
    from plutus import db
    importlib.reload(db)
    from plutus.sources import gmgn
    from plutus.track import trackers as T

    tid = db.upsert_token("robinhood", "0x" + "a" * 40, symbol="T")
    db.record_pool(tid, "0x" + "d" * 40, 1e6, 5e4, 1e5, "sim")
    N = 120
    for i in range(N):
        db.classify(tid, "0x%040x" % i, "ours", source="operator")

    calls = []
    gmgn.token_balance = lambda c, w, t, fresh=False, **k: (calls.append(w), time.sleep(0.05),
                                                       (1.0, 1))[2]
    T.clear_abort(tid)
    threading.Thread(target=lambda: (time.sleep(0.3), T.abort(tid)), daemon=True).start()
    r = T.track_inventory(tid, full=True)
    assert len(calls) < N, \
        f"the sweep made all {N} calls after the token was deleted — abort never reached it"
    assert not r.ok and "ABORT" in r.detail.upper(), \
        f"an aborted sweep reported success: {r.detail}"


def test_re_onboarding_a_deleted_token_costs_nothing_inside_the_ttl():
    """DELETE IS STILL GONE. The cache holds vendor answers, not the token."""
    gmgn, _ = _fresh(hour="500")
    real = []
    gmgn._http_call = lambda args, **k: (real.append(args), {"ok": True})[1]
    args = ("token", "info", "--chain", "robinhood", "--address", "0x" + "a" * 40)

    gmgn.call(*args)
    assert len(real) == 1, "the first call should reach the vendor"
    for _ in range(5):
        gmgn.call(*args)                    # delete + re-onboard, five times over
    assert len(real) == 1, \
        f"re-onboarding cost {len(real) - 1} extra calls — the cache is not surviving delete"


def test_an_explicit_pull_is_never_served_from_cache():
    gmgn, _ = _fresh(hour="500")
    real = []
    gmgn._http_call = lambda args, **k: (real.append(args), {"ok": True})[1]
    args = ("portfolio", "token-balance", "--chain", "robinhood",
            "--wallet", "0x" + "b" * 40, "--token", "0x" + "a" * 40)
    gmgn.call(*args)
    gmgn.call(*args)
    assert len(real) == 1, "the background path should have used the cache"
    gmgn.call(*args, fresh=True)
    assert len(real) == 2, \
        "fresh=True was served from cache — an operator asking for current data got a copy"


def test_a_price_quote_is_never_cached():
    gmgn, _ = _fresh()
    assert gmgn._cache_ttl(("order", "quote")) == 0, \
        "swap quotes must never be served stale; a price is the one thing that cannot be a copy"


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
