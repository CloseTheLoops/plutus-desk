"""The balance sweep runs concurrently; these are the two properties that makes safe.

WHY THIS EXISTS. Sequentially the sweep costs ~0.57s per wallet -- almost all of it spawning a
Node process -- so 155 wallets take ~88s, during which every derived figure on the page reads
zero. Concurrency cuts that to ~14s. It also introduces two ways to be wrong that a timing
measurement would not catch:

  1. the rate pacer stops working, because each thread reads the same `_last_call`, computes the
     same gap, sleeps it, and then fires together -- the exact burst the interval prevents;
  2. results get attached to the wrong wallet, which would silently misreport who holds what.

Both are asserted here without touching the network.
"""
from __future__ import annotations

import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plutus.sources import gmgn  # noqa: E402
from plutus.track import trackers  # noqa: E402


def test_pacer_holds_the_interval_across_threads():
    stamps: list[float] = []
    lock = threading.Lock()

    def hit(_):
        gmgn._pace()
        with lock:
            stamps.append(time.perf_counter())

    with ThreadPoolExecutor(max_workers=trackers.BALANCE_WORKERS) as ex:
        list(ex.map(hit, range(24)))

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    floor = gmgn.MIN_INTERVAL_S
    bad = [g for g in gaps if g < floor * 0.9]
    assert not bad, (
        f"{len(bad)} of {len(gaps)} gaps were under the {floor * 1000:.0f}ms floor "
        f"(min {min(gaps) * 1000:.1f}ms) — the pacer is not holding across threads, so the "
        f"sweep can burst straight through the rate limit")


def test_pacer_is_actually_serialising_not_just_sleeping():
    """24 calls at a 60ms floor cannot finish faster than ~23 intervals."""
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=trackers.BALANCE_WORKERS) as ex:
        list(ex.map(lambda _: gmgn._pace(), range(24)))
    took = time.perf_counter() - t0
    floor = 23 * gmgn.MIN_INTERVAL_S
    assert took >= floor * 0.9, (
        f"24 paced calls took {took * 1000:.0f}ms, under the {floor * 1000:.0f}ms the interval "
        f"requires — threads are slipping past the lock")


def test_map_preserves_order_so_results_match_their_wallet():
    """The sweep zips results back to `targets` positionally. If ThreadPoolExecutor.map ever
    returned completion order instead of input order, every balance would be filed under the
    wrong wallet — silently, and in a way no total would reveal."""
    import random
    wallets = [f"0x{i:040x}" for i in range(40)]

    def slow(w):
        time.sleep(random.uniform(0, 0.01))     # finish out of order on purpose
        return w

    with ThreadPoolExecutor(max_workers=8) as ex:
        got = list(ex.map(slow, wallets))
    assert got == wallets, "ThreadPoolExecutor.map did not preserve input order"


def test_worker_count_stays_under_the_pacer_ceiling():
    """More workers than the pacer allows per second buys nothing and only adds contention."""
    ceiling = 1 / gmgn.MIN_INTERVAL_S
    assert trackers.BALANCE_WORKERS <= ceiling, (
        f"BALANCE_WORKERS={trackers.BALANCE_WORKERS} exceeds the pacer's "
        f"{ceiling:.0f}/s ceiling")


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
