"""Background tasks must be owned, unstackable, and cancelled when their token goes away.

THE THREE FAILURES THIS PINS DOWN, all observed in a live session:

  * `create_task(...)` with the result discarded. Nothing could cancel the task, and asyncio may
    garbage-collect a task no one holds a reference to -- a tracker that stops silently.
  * Onboarding a token already being tracked started a SECOND loop on the same id. Two loops
    means double the API calls and two inventory sweeps interleaving writes into one table.
  * Deleting a token cancelled nothing. The orphaned loop kept ticking against a row that no
    longer existed and logged `unknown token 1` every cycle for the life of the process, because
    the loop is deliberately written to survive any error -- including being pointless.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plutus import config  # noqa: E402

_DB = pathlib.Path(tempfile.gettempdir()) / "plutus_test_lifecycle.db"


def _fresh_db():
    try:
        from plutus import db as _prev
        if getattr(_prev, "_conn", None) is not None:
            _prev._conn.close()
            _prev._conn = None
    except ImportError:
        pass
    for s in ("", "-wal", "-shm"):
        pathlib.Path(str(_DB) + s).unlink(missing_ok=True)
    config.DB_PATH = str(_DB)
    import importlib

    from plutus import db as _db
    importlib.reload(_db)
    return _db


def test_register_refuses_to_stack_a_second_task():
    from plutus.web import app

    async def go():
        app._loops.clear()
        started = []

        async def work(tag):
            started.append(tag)
            await asyncio.sleep(5)

        first = app._register(app._loops, 1, work("a"))
        second = app._register(app._loops, 1, work("b"))
        await asyncio.sleep(0.05)
        assert first is not None, "the first task should have started"
        assert second is None, "a second task was started for a token already running one"
        assert started == ["a"], f"the refused coroutine still ran: {started}"
        app._stop_tasks(1)

    asyncio.run(go())


def test_register_allows_a_replacement_once_the_first_finishes():
    from plutus.web import app

    async def go():
        app._loops.clear()

        async def quick():
            return

        assert app._register(app._loops, 2, quick()) is not None
        await asyncio.sleep(0.05)
        assert 2 not in app._loops, "a finished task was not dropped from the registry"
        assert app._register(app._loops, 2, quick()) is not None, \
            "could not start a new task after the previous one completed"
        await asyncio.sleep(0.05)

    asyncio.run(go())


def test_stop_tasks_cancels_and_reports():
    from plutus.web import app

    async def go():
        app._loops.clear(); app._scans.clear()

        async def forever():
            await asyncio.sleep(60)

        app._register(app._loops, 3, forever())
        app._register(app._scans, 3, forever())
        await asyncio.sleep(0.05)
        n = app._stop_tasks(3)
        assert n == 2, f"expected to cancel 2 tasks, cancelled {n}"
        await asyncio.sleep(0.05)
        assert 3 not in app._loops and 3 not in app._scans, "registry still holds the token"

    asyncio.run(go())


def test_loop_exits_when_its_token_is_deleted():
    """The self-healing half: even if a cancel is missed, the loop must not run forever."""
    db = _fresh_db()
    from plutus.web import app

    tid = db.upsert_token("robinhood", "0x" + "a" * 40, symbol="GONE")

    async def go():
        db.delete_token(tid)
        # the loop should return on its first cycle rather than tick against a missing row
        await asyncio.wait_for(app._loop(tid), timeout=5)

    asyncio.run(go())


def test_delete_endpoint_cancels_before_removing_rows():
    """Ordering matters: deleting rows under a running loop is what produced the endless error."""
    import inspect

    from plutus.web import app
    src = inspect.getsource(app.api_delete_token)
    assert "_stop_tasks" in src, "the delete endpoint does not cancel background tasks"
    assert src.index("_stop_tasks") < src.index("db.delete_token"), \
        "tasks must be cancelled BEFORE the token's rows are deleted"


def test_onboard_refuses_a_concurrent_discovery():
    import inspect

    from plutus.web import app
    src = inspect.getsource(app.api_onboard)
    assert "_onboarding" in src, "onboard has no guard against a concurrent run"
    assert "status_code=409" in src, "a duplicate onboard should be refused, not queued"
    assert "finally:" in src, "the in-flight marker must be cleared even when discovery raises"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except (AssertionError, asyncio.TimeoutError) as exc:
                fails += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
