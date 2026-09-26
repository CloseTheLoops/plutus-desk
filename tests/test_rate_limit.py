"""The pacer must hold the interval across PROCESSES, not just threads.

WHY A THREADING LOCK WAS NOT ENOUGH. The interval exists to stay inside what the API key is
allowed. A `threading.Lock` serialises calls inside one interpreter and knows nothing about any
other. The service tracks tokens continuously, and an operator can run `plutus.cli tick` beside
it: two processes, two private clocks, one key, twice the intended rate. Nothing in either
process can see that happening.

So the state lives in the SQLite database both processes already open. These tests check the
property that matters -- merge the call timestamps from two separate OS processes and no two are
closer together than the interval -- and the thing that property must not cost: the pacer must
never hold SQLite's write lock while it sleeps, or every unrelated write queues behind it.
"""
from __future__ import annotations

import os as _os_guard
_os_guard.environ.setdefault("PLUTUS_ETHERSCAN_DISABLE", "1")   # never real Etherscan here
_os_guard.environ.setdefault("PLUTUS_RPC_DISABLE", "1")          # never a real chain node here

import pathlib
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# NEVER THE PRODUCTION DATABASE -- including in the worker processes, which inherit this
# environment. They used to open the server's own database and spend its real budget.
import os as _os  # noqa: E402
_os.environ["PLUTUS_DB"] = str(pathlib.Path(tempfile.gettempdir())
                               / f"plutus_ratelimit_{_os.getpid()}.db")
_os.environ.setdefault("PLUTUS_MAX_CALLS_HOUR", "1000000")
_os.environ.setdefault("PLUTUS_MAX_CALLS_DAY", "10000000")
from plutus import config  # noqa: E402
config.DB_PATH = pathlib.Path(_os.environ["PLUTUS_DB"])
from plutus.sources import gmgn  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

WORKER = textwrap.dedent("""
    import json, sys, time
    sys.path.insert(0, %r)
    from plutus.sources import gmgn
    if len(sys.argv) > 2 and sys.argv[2] == "legacy":
        # The pre-fix pacer: a clock private to this process, blind to any other.
        import threading
        _lk, _last = threading.Lock(), [0.0]
        def _legacy():
            with _lk:
                gap = gmgn.MIN_INTERVAL_S - (time.time() - _last[0])
                if gap > 0:
                    time.sleep(gap)
                _last[0] = time.time()
        gmgn._pace = _legacy
    # Start together. Process startup on Windows can take long enough that one worker finishes
    # before the other begins -- then the two never overlap, the rate is never doubled, and the
    # control test failed for a reason that had nothing to do with the pacer.
    start_at = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    time.sleep(max(0.0, start_at - time.time()))
    out = []
    for _ in range(int(sys.argv[1])):
        gmgn._pace()
        out.append(time.time())
    print(json.dumps(out))
""") % str(ROOT)


def _two_processes(n: int, mode: str = "shared") -> list[float]:
    script = pathlib.Path(tempfile.gettempdir()) / f"plutus_pace_{mode}.py"
    script.write_text(WORKER, encoding="utf-8")
    start_at = time.time() + 3.0
    args = [sys.executable, str(script), str(n), "legacy" if mode == "legacy" else "shared",
            f"{start_at:.3f}"]
    procs = [subprocess.Popen(args, stdout=subprocess.PIPE, text=True) for _ in range(2)]
    stamps: list[float] = []
    for p in procs:
        out, _ = p.communicate(timeout=120)
        assert p.returncode == 0, f"worker failed: {out[:300]}"
        stamps += json.loads(out.strip().splitlines()[-1])
    script.unlink(missing_ok=True)
    return sorted(stamps)


def _rate(stamps: list[float]) -> float:
    span = stamps[-1] - stamps[0]
    return (len(stamps) - 1) / span if span > 0 else float("inf")


def test_two_processes_stay_under_the_rate_cap():
    """The property that actually matters: the API key sees no more than the cap.

    Note on what is NOT asserted. Individual gaps jitter by a few milliseconds, because
    time.sleep guarantees *at least* its argument, so a call can fire late and the next one
    punctually -- compressing the measured gap even though both reservations were exactly one
    interval apart. Chasing that is chasing the OS scheduler. The rate over the run is the
    constraint the vendor applies, so it is the one asserted.
    """
    n = 14
    stamps = _two_processes(n)
    assert len(stamps) == 2 * n
    cap = 1 / gmgn.MIN_INTERVAL_S
    got = _rate(stamps)
    assert got <= cap * 1.15, (
        f"two processes sustained {got:.1f} calls/s against a {cap:.1f}/s cap — they are not "
        f"sharing the clock")
    worst = min(b - a for a, b in zip(stamps, stamps[1:]))
    # 0.8, not 0.99: time.sleep guarantees only a lower bound and two OS processes jitter
    # against each other by a millisecond or two. A real coordination failure is not
    # subtle -- it puts calls microseconds apart, as the control test below demonstrates.
    assert worst > gmgn.MIN_INTERVAL_S * 0.8, (
        f"closest two calls were {worst * 1000:.1f}ms apart, under half the "
        f"{gmgn.MIN_INTERVAL_S * 1000:.0f}ms interval — that is a coordination failure, not "
        f"scheduling jitter")


def test_the_previous_in_process_pacer_would_fail_this():
    """A control. Without it, the test above could be passing for the wrong reason.

    Two processes each running the old private-clock pacer must visibly exceed the cap. If this
    ever stops failing, the test above has stopped measuring anything.
    """
    n = 14
    shared = _rate(_two_processes(n, "shared"))
    legacy = _rate(_two_processes(n, "legacy"))
    cap = 1 / gmgn.MIN_INTERVAL_S
    assert legacy > cap * 1.4, (
        f"the legacy per-process pacer sustained {legacy:.1f}/s, which does not exceed the "
        f"{cap:.1f}/s cap enough to prove the shared clock is doing anything")
    assert legacy > shared * 1.4, (
        f"shared clock {shared:.1f}/s vs private clock {legacy:.1f}/s — the fix is not "
        f"measurably changing the rate")


def test_pacer_does_not_hold_the_write_lock_while_sleeping():
    """The failure this rules out: unrelated writes queueing behind the rate limiter."""
    import sqlite3

    from plutus import config

    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            gmgn._pace()

    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)                      # let the pacers get going

    c = sqlite3.connect(str(config.DB_PATH), timeout=10, check_same_thread=False,
                        isolation_level=None)
    c.execute("PRAGMA busy_timeout=3000")
    c.execute("CREATE TABLE IF NOT EXISTS _pace_probe (k INTEGER PRIMARY KEY, v REAL)")
    worst = 0.0
    try:
        for i in range(15):
            t0 = time.perf_counter()
            c.execute("INSERT OR REPLACE INTO _pace_probe (k, v) VALUES (?, ?)", (i, time.time()))
            worst = max(worst, time.perf_counter() - t0)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)
        c.execute("DROP TABLE IF EXISTS _pace_probe")
        c.close()

    assert worst < 0.5, (
        f"an unrelated write waited {worst * 1000:.0f}ms while the pacer was busy — the pacer is "
        f"holding SQLite's writer lock across its sleep, so real work queues behind it")


def test_reservation_moves_the_clock_forward_monotonically():
    a = gmgn._pace_db().execute("SELECT next_free FROM rate_state WHERE k='gmgn'").fetchone()
    gmgn._pace()
    b = gmgn._pace_db().execute("SELECT next_free FROM rate_state WHERE k='gmgn'").fetchone()
    assert b is not None and (a is None or b[0] >= a[0]), \
        "next_free went backwards, so a later caller could be given an earlier slot"


def test_a_far_future_clock_does_not_stall_everything():
    """A crashed reservation or a clock change must not wedge the pacer for its duration."""
    c = gmgn._pace_db()
    c.execute("INSERT INTO rate_state (k, next_free) VALUES ('gmgn', ?) "
              "ON CONFLICT(k) DO UPDATE SET next_free=excluded.next_free",
              (time.time() + 3600,))
    t0 = time.perf_counter()
    gmgn._pace()
    took = time.perf_counter() - t0
    assert took < 1.0, (
        f"a next_free one hour in the future stalled the pacer for {took:.1f}s — it must treat "
        f"an implausible clock as now, not wait it out")


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
