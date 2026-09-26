"""The simulated week again, with the transfer ledger on.

Same world, same events -- buy wave, transfers between our own wallets, staking, sell wave,
restarts, 429s, another process on the key -- but balances now come from the token's transfer
log (simulated Etherscan) instead of per-wallet GMGN reads. What should change, and is asserted:

  * the position is EXACT at every hour -- transfers and staking included, which per-wallet
    reads took one to two hours to catch;
  * GMGN calls fall to a handful an hour (holder tags every few hours, the pool check);
  * Etherscan stays far inside its budget.
"""
from __future__ import annotations

import os as _os_guard
_os_guard.environ.setdefault("PLUTUS_ETHERSCAN_DISABLE", "1")

import pathlib
import sys
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import test_week as W  # noqa: E402

from test_week import CLOCK, DAY, HOUR, T0  # noqa: E402

E = W.T.etherscan
db, T, L = W.db, W.T, W.L


class LedgerHarness(W.Harness):
    def install(self):
        super().install()
        w, h = self.w, self
        h.es = defaultdict(int)
        h.last_sync = T0
        h.moves: list[tuple] = []

        def counted(fn):
            def inner(*a, **k):
                h.es[h.hour()] += 1
                return fn(*a, **k)
            return inner

        def head(cid):
            h.last_sync = CLOCK[0]
            return w.block()

        E.api_key = lambda: "sim-key"
        E.decimals = counted(lambda cid, tok: 18)
        E.latest_block = counted(head)
        E.token_supply = counted(lambda cid, tok: w.minted_raw)
        E.transfer_logs = counted(lambda cid, tok, lo, hi: w.etherscan_logs(lo, hi))

        def forbidden(*a, **k):
            raise AssertionError("a simulation reached the real Etherscan API")
        E._get = forbidden

        real_transfer = w.transfer

        def transfer(a, b, amount):                     # remember non-trade moves for fair truth
            amount = min(amount, w.bal.get(a, 0.0))
            h.moves.append((CLOCK[0], a, b, amount))
            return real_transfer(a, b, amount)
        w.transfer = transfer

    def onboard(self):
        tid = db.upsert_token(W.CHAIN, W.TOKEN, symbol="SIM", supply_nominal=W.SUPPLY,
                              primary_venue=W.PID)
        for a in self.w.ours:
            db.classify(tid, a, "ours", source="operator")
        db.classify(tid, W.POOL, "pool", source="operator")
        db.classify(tid, W.STAKE, "locked", source="operator")
        db.classify(tid, W.DEAD, "burnt", source="operator")
        T.clear_abort(tid)
        errs, self.w.errors_p = self.w.errors_p, 0.0
        T.track_tape(tid)
        r = T.track_ledger(tid)                         # as app._onboard_scan does with a key
        assert r.ok, f"ledger backfill failed: {r.detail}"
        T.track_pool(tid, True)
        T.track_census(tid)
        self.w.errors_p = errs
        self.tid = tid

    def _full_pull(self):
        # As app.api_refresh now does when a ledger exists: sync it, no wallet sweep.
        self.full_pull_result = T.track_ledger(self.tid)

    def snapshot(self):
        super().snapshot()
        # Fair truth for the ledger: a transfer after the last sync could not be known yet.
        s = self.hourly[-1]
        ours = set(self.w.ours)
        adj = 0.0
        for ts, a, b, amt in self.moves:
            if ts > self.last_sync:
                adj += (amt if b in ours else 0.0) - (amt if a in ours else 0.0)
        truth = s["truth"] - adj
        s["err"] = abs(s["ours"] - truth) / truth
        s["exact"] = any("exact" in n for n in s["notes"])


def run_week_ledger():
    h = LedgerHarness()
    h.install()
    h.onboard()
    h.script()
    h.run()
    return h


def test_a_week_with_the_transfer_ledger():
    h = run_week_ledger()
    gm = [h.calls[x]["bg"] + h.calls[x]["op"] for x in range(168)]
    es = [h.es[x] for x in range(168)]
    worst = max(h.hourly, key=lambda s: s["err"])
    exact_share = sum(1 for s in h.hourly if s["exact"]) / len(h.hourly)
    print(f"  position error vs chain: worst {worst['err']:.4%} (hour {worst['hour']})")
    print(f"  hours on exact balances: {exact_share:.0%}")
    print(f"  GMGN calls/hour: median {sorted(gm[2:])[len(gm[2:]) // 2]}, max {max(gm[2:])} "
          f"(was ~110 median with per-wallet reads)")
    print(f"  Etherscan calls/day: {sum(es) / 7:,.0f} (plan allows 100,000)")
    per_day = Counter(int((ts - T0) // DAY) for ts, _l, _m in h.log)
    print(f"  warnings per day: max {max(per_day.values(), default=0)}")

    assert worst["err"] < 0.001, f"with the ledger the position was off by {worst['err']:.3%}"
    assert exact_share > 0.95, f"exact balances only {exact_share:.0%} of hours"
    assert sorted(gm[2:])[len(gm[2:]) // 2] <= 20, "GMGN still carries balance reads"
    assert sum(es) / 7 < 10_000, "the ledger spends too much of the Etherscan budget"
    assert not any("past their" in n or "outside the trade feed" in n
                   for s in h.hourly for n in s["notes"]), "per-wallet warnings in ledger mode"
    errors = [m for _t, lvl, m in h.log if lvl in ("ERROR", "CRITICAL")]
    assert not errors, f"errors logged: {errors[:3]}"


if __name__ == "__main__":
    try:
        test_a_week_with_the_transfer_ledger()
        print()
        print("ALL PASS")
    except AssertionError as exc:
        print()
        print(f"FAIL  {exc}")
        sys.exit(1)
