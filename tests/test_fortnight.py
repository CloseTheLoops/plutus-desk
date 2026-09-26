"""Two tokens sharing one API key, for two simulated weeks.

WHY. test_week runs one token for a week. What it cannot show: tokens COMPETING for one budget --
one token's catch-up re-reads starving another's census -- and what the second week looks like,
after retention has had to work and the tables have grown.

Runs the REAL tracker loop once per token, concurrently, on a virtual-time scheduler: each loop
sleeps on the fake clock and is woken in time order, exactly as two coroutines would be. Opt-in,
because it takes several minutes:  PLUTUS_LONG_TESTS=1 python tests/test_fortnight.py
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import os
import pathlib
import sys
import time as _time_module
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
LONG = os.environ.get("PLUTUS_LONG_TESTS", "").strip() in ("1", "true", "yes")

import test_week as W  # noqa: E402  -- installs the fake clock, test database and caps

from test_week import CHAIN, CLOCK, DAY, DEAD, HOUR, POOL, SUPPLY, T0, RestartSim, StopSim  # noqa: E402

db, gmgn, gecko, T, app, L = W.db, W.gmgn, W.gecko, W.T, W.app, W.L
DAYS = 14


class Fortnight:
    def __init__(self):
        self.worlds = {
            "A": W.World(seed=7, token="0x" + "7" * 40, pid="0x" + "e" * 64,
                         stake="0x" + "5" * 40, n_ours=458, n_third=300,
                         ours_base=0x1000, third_base=0x900000),
            "B": W.World(seed=11, token="0x" + "8" * 40, pid="0x" + "f" * 64,
                         stake="0x" + "6" * 40, n_ours=150, n_third=200,
                         ours_base=0x5000, third_base=0xA00000),
        }
        self.by_token = {w.token: w for w in self.worlds.values()}
        self.by_pid = {w.pid: w for w in self.worlds.values()}
        self.tid: dict[str, int] = {}
        self.calls = defaultdict(Counter)
        self.refused = defaultdict(Counter)
        self.log: list = []
        self.snaps = defaultdict(list)
        self.last_tape: dict[str, float] = {}
        self.events: list = []
        self.next_hour = T0 + HOUR
        self.q: list = []
        self.seq = itertools.count()
        self.restarts = 0
        self.build_ms: list[float] = []
        self.page_ms: dict[str, list[float]] = defaultdict(list)

    # ── vendor and free sources, routed by token ─────────────────────────────────────
    def install(self):
        h = self

        def http(args, background=False):
            gmgn._pace(background)
            flags = {args[i]: args[i + 1] for i in range(2, len(args) - 1, 2)}
            w = h.by_token.get(flags.get("--token") or flags.get("--address"))
            if w is None:
                raise AssertionError(f"unrouted vendor call {args}")
            if w.rng.random() < w.rate_limit_p:
                gmgn.pause_all(35, "sim")
                raise gmgn.RateLimited("RATE_LIMIT sim")
            if w.rng.random() < w.errors_p:
                raise gmgn.Transient("http 500 sim")
            return w.answer(args)

        real_reserve = gmgn._reserve_slot

        def counted(background=False):
            try:
                wait = real_reserve(background)
            except gmgn.BudgetExceeded:
                h.refused[h.hour()]["bg" if background else "op"] += 1
                raise
            h.calls[h.hour()]["bg" if background else "op"] += 1
            return wait

        def trades(net, pool, quick=False):
            h.last_tape[pool] = CLOCK[0]
            return h.by_pid[pool].gecko_trades()

        gmgn._http_call = http
        gmgn._reserve_slot = counted
        gecko.trades = trades
        gecko.pool = lambda net, pid: h.by_pid[pid].gecko_pool()

        import logging

        class Grab(logging.Handler):
            def emit(self, rec):
                if rec.levelno >= logging.WARNING:
                    h.log.append((CLOCK[0], rec.levelname, rec.getMessage()[:200]))
        logging.getLogger().addHandler(Grab())

    def hour(self) -> int:
        return int((CLOCK[0] - T0) // HOUR)

    # ── onboarding, as app._onboard_scan does it ─────────────────────────────────────
    def onboard(self):
        for name, w in self.worlds.items():
            tid = db.upsert_token(CHAIN, w.token, symbol=name, supply_nominal=SUPPLY,
                                  primary_venue=w.pid)
            for a in w.ours:
                db.classify(tid, a, "ours", source="operator")
            db.classify(tid, POOL, "pool", source="operator")
            db.classify(tid, w.stake, "locked", source="operator")
            db.classify(tid, DEAD, "burnt", source="operator")
            T.clear_abort(tid)
            errs, w.errors_p = w.errors_p, 0.0
            T.track_tape(tid)
            assert T.track_inventory(tid, full=True).ok
            T.track_pool(tid, True)
            T.track_census(tid)
            w.errors_p = errs
            self.tid[name] = tid

    # ── two weeks of events ──────────────────────────────────────────────────────────
    def script(self):
        A, B = self.worlds["A"], self.worlds["B"]
        for w, mean in ((A, 300), (B, 600)):
            rng, t = w.rng, T0
            while t < T0 + DAYS * DAY:
                t += rng.expovariate(1 / mean)
                m = rng.choice(w.third)
                self.at(t, (lambda w=w, m=m: w.buy(m, w.rng.uniform(10, 400)))
                        if rng.random() < 0.55 else
                        (lambda w=w, m=m: w.sell(m, w.bal.get(m, 0) * w.rng.uniform(0.05, 0.4))))
        for wk in (0, 7):
            base = T0 + wk * DAY
            for i, o in enumerate(A.ours):
                self.at(base + DAY + 12 * HOUR + i * 90 / len(A.ours), lambda o=o: A.buy(o, 20.0))
            for i in range(20):
                a, b = A.ours[i + wk], A.ours[-1 - i - wk]
                self.at(base + 2 * DAY + 6 * HOUR + i, lambda a=a, b=b: A.transfer(a, b, A.bal[a] * 0.5))
            for i, o in enumerate(B.ours):
                self.at(base + 3 * DAY + 15 * HOUR + i * 0.3, lambda o=o: B.buy(o, 15.0))
            for i in range(10):
                a, b = B.ours[i], B.ours[-1 - i]
                self.at(base + 6 * DAY + 2 * HOUR + i, lambda a=a, b=b: B.transfer(a, b, B.bal[a] * 0.4))
            for hr in range(6):                           # another process on the same key
                for k in range(400):
                    self.at(base + 3 * DAY + (14 + hr) * HOUR + k * 9, self._external)
            for i in range(50):
                o = A.ours[100 + i]
                self.at(base + 4 * DAY + 10 * HOUR + i * 5, lambda o=o: A.transfer(o, A.stake, A.bal[o] * 0.3))
        self.at(T0 + 3 * DAY + 9 * HOUR, lambda: [setattr(w, "rate_limit_p", 0.1) for w in (A, B)])
        self.at(T0 + 3 * DAY + 10 * HOUR, lambda: [setattr(w, "rate_limit_p", 0.0) for w in (A, B)])
        for d in (2.75, 9.4, 13.1):
            self.at(T0 + d * DAY, "restart")
        self.events.sort(key=lambda e: e[0])

    def at(self, ts, fn):
        self.events.append((ts, fn))

    def _external(self):
        try:
            gmgn._reserve_slot(False)
        except gmgn.BudgetExceeded:
            pass

    # ── virtual time ─────────────────────────────────────────────────────────────────
    async def vsleep(self, s):
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self.q, (CLOCK[0] + max(0.0, s), next(self.seq), fut))
        await fut

    def advance_to(self, wake):
        while self.events and self.events[0][0] <= wake:
            ts, fn = self.events.pop(0)
            CLOCK[0] = max(CLOCK[0], ts)
            if fn == "restart":
                raise RestartSim()
            fn()
        CLOCK[0] = max(CLOCK[0], wake)
        while CLOCK[0] >= self.next_hour:
            self.snapshot()
            self.next_hour += HOUR
        if CLOCK[0] >= T0 + DAYS * DAY:
            raise StopSim()

    def snapshot(self):
        for name, w in self.worlds.items():
            tid = self.tid[name]
            t0 = _time_module.perf_counter()
            led = L.build(tid)
            self.build_ms.append((_time_module.perf_counter() - t0) * 1000)
            # What the pages actually load, every 30s, on a database that keeps growing.
            for page, fn in (("analysis", lambda: app.snapshot(tid)),
                             ("holders", lambda: app.api_holders(tid)),):
                p0 = _time_module.perf_counter()
                fn()
                self.page_ms[page].append((_time_module.perf_counter() - p0) * 1000)
            truth = w.true_ours()
            seen = self.last_tape.get(w.pid, CLOCK[0])
            ours = set(w.ours)
            for tr in reversed(w.trades):
                if tr["ts"] <= seen:
                    break
                if tr["maker"] in ours:
                    truth -= tr["base_amount"] if tr["side"] == "buy" else -tr["base_amount"]
            rows = db.latest_balance_rows(tid)
            stale = max((CLOCK[0] - rows[a][2]) for a in w.ours if a in rows)
            meta = db.latest_census_meta(tid)
            self.snaps[name].append({
                "hour": self.hour(), "err": abs(led.ours - truth) / truth,
                "stale_h": stale / HOUR, "notes": list(led.notes),
                "census_age_h": (CLOCK[0] - meta["sweep_ts"]) / HOUR if meta else 99})

    def run(self):
        h = self
        real_sleep, real_thread = asyncio.sleep, asyncio.to_thread

        async def inline(fn, /, *a, **k):
            return fn(*a, **k)

        asyncio.sleep, asyncio.to_thread = self.vsleep, inline

        async def main():
            while True:
                app._jobs.clear(); app._scans.clear(); app._loops.clear()
                h.q.clear()
                tasks = [asyncio.create_task(app._loop(tid)) for tid in h.tid.values()]
                try:
                    while True:
                        for _ in range(4):
                            await real_sleep(0)
                        for t in tasks:
                            if t.done():
                                t.result()                # a loop that died is a failure
                        wake, _s, fut = heapq.heappop(h.q)
                        h.advance_to(wake)
                        if not fut.done():
                            fut.set_result(None)
                except RestartSim:
                    h.restarts += 1
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                except StopSim:
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    return
        try:
            asyncio.run(main())
        finally:
            asyncio.sleep, asyncio.to_thread = real_sleep, real_thread


def run_fortnight():
    f = Fortnight()
    f.install()
    f.onboard()
    f.script()
    f.run()
    return f


def report(f) -> str:
    bg = [f.calls[x]["bg"] for x in range(DAYS * 24)]
    lines = [f"  both tokens, background calls/hour: median {sorted(bg[13:])[len(bg[13:]) // 2]}, "
             f"max {max(bg)} (ceiling 450)",
             f"  operator refusals: {sum(f.refused[x]['op'] for x in f.refused)}"]
    for name, snaps in f.snaps.items():
        run = longest = 0
        for s in snaps:
            run = run + 1 if s["err"] > 0.001 else 0
            longest = max(longest, run)
        lines.append(f"  token {name}: worst error {max(s['err'] for s in snaps):.2%}, longest "
                     f"run {longest}h, stalest {max(s['stale_h'] for s in snaps[13:]):.1f}h, "
                     f"oldest census {max(s['census_age_h'] for s in snaps[2:]):.1f}h")
    lines.append(f"  ledger build time: median {sorted(f.build_ms)[len(f.build_ms) // 2]:.0f}ms, "
                 f"last {f.build_ms[-1]:.0f}ms")
    for page, ms in f.page_ms.items():
        lines.append(f"  {page} page data: median {sorted(ms)[len(ms) // 2]:.0f}ms, "
                     f"worst {max(ms):.0f}ms, last day median "
                     f"{sorted(ms[-48:])[len(ms[-48:]) // 2]:.0f}ms")
    per_day = Counter(int((ts - T0) // DAY) for ts, _l, _m in f.log)
    lines.append(f"  warnings per day: max {max(per_day.values(), default=0)}")
    lines.append(f"  restarts survived: {f.restarts}")
    lines.append("  table rows: " + ", ".join(
        f"{t}={db.connect().execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}"
        for t in ("trades", "balances", "census", "pool_obs", "drift")))
    size = pathlib.Path(str(W.config.DB_PATH)).stat().st_size / 1e6
    lines.append(f"  database: {size:.1f} MB")
    return "\n".join(lines)


def test_two_tokens_for_two_weeks():
    if not LONG:
        print("  long test — skipped (set PLUTUS_LONG_TESTS=1 to run)")
        return
    f = run_fortnight()
    print(report(f))
    bg = [f.calls[x]["bg"] for x in range(DAYS * 24)]
    assert max(bg) <= 450, f"background reached {max(bg)}/h across both tokens"
    assert sum(f.refused[x]["op"] for x in f.refused) == 0, "an operator action was refused"
    assert f.restarts == 3
    for name, snaps in f.snaps.items():
        run = longest = 0
        for s in snaps:
            run = run + 1 if s["err"] > 0.001 else 0
            longest = max(longest, run)
        assert longest <= 2, f"token {name}: position off for {longest}h in a row"
        assert max(s["err"] for s in snaps) < 0.05, f"token {name}: error reached 5%"
        for s in snaps[13:]:
            if s["stale_h"] > T.RECONCILE_S / 3600 + 0.2:
                assert any("past their" in n for n in s["notes"]), \
                    f"token {name} hour {s['hour']}: overdue wallets not reported"
        worst_stale = max(s["stale_h"] for s in snaps[13:])
        assert worst_stale <= T.HARD_STALE_S / 3600 + 1,             f"token {name}: a wallet went {worst_stale:.1f}h unread (hard limit {T.HARD_STALE_S / 3600:.0f}h)"
        worst_census = max(s["census_age_h"] for s in snaps[2:])
        assert worst_census <= 8, f"token {name}: census reached {worst_census:.1f}h old"
    assert sorted(f.build_ms)[len(f.build_ms) // 2] < 500, "the ledger got slow as data grew"
    for page, ms in f.page_ms.items():
        late = sorted(ms[-48:])[len(ms[-48:]) // 2]
        assert late < 1000, f"the {page} page takes {late:.0f}ms to load after two weeks"
    errors = [m for _t, lvl, m in f.log if lvl in ("ERROR", "CRITICAL")]
    assert not errors, f"errors logged: {errors[:3]}"


if __name__ == "__main__":
    if not LONG:
        print("  long test — skipped (set PLUTUS_LONG_TESTS=1 to run)")
        sys.exit(0)
    try:
        test_two_tokens_for_two_weeks()
        print()
        print("ALL PASS")
    except AssertionError as exc:
        print()
        print(f"FAIL  {exc}")
        sys.exit(1)
