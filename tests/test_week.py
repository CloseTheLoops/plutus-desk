"""A simulated WEEK of running, against a simulated chain whose true balances are always known.

WHY. The desk is left running for days. Bugs that matter there -- a budget that drains overnight,
a position that drifts, a table that grows without end, a warning logged every minute -- do not
show up in a test that runs one step. This runs the REAL tracker loop (app._loop), the real budget
and pacer, the real trackers and the real ledger for seven simulated days, with only the chain and
the vendor simulated, and checks the desk's view against the chain's truth every hour.

The week includes what actually happens: third-party trading throughout, a 458-wallet buy wave
fast enough to overflow the trade feed, transfers between our own wallets, staking, a sell wave,
two server restarts, an hour of 429s, another process burning calls on the same key, an operator
full pull, and a whale.

Time is simulated: `time.time` and `time.sleep` run on a fake clock, `asyncio.sleep` advances it,
and threads run inline, so a week takes about a minute and every run is identical.
"""
from __future__ import annotations

import os as _os_guard
_os_guard.environ.setdefault("PLUTUS_ETHERSCAN_DISABLE", "1")   # never real Etherscan here
_os_guard.environ.setdefault("PLUTUS_RPC_DISABLE", "1")          # never a real chain node here

import asyncio
import importlib
import logging
import os
import pathlib
import random
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ["PLUTUS_DB"] = str(pathlib.Path(tempfile.gettempdir())
                              / f"plutus_week_{os.getpid()}_{time.time_ns()}.db")
os.environ["PLUTUS_MAX_CALLS_HOUR"] = "900"
os.environ["PLUTUS_MAX_CALLS_DAY"] = "10000"
os.environ["PLUTUS_BALANCE_WORKERS"] = "1"          # deterministic: one reader thread
os.environ["PLUTUS_NO_CACHE"] = "1"                 # count every call the loop would make

# ── the fake clock, installed before anything reads the time ─────────────────────────
T0 = 1_800_000_000.0
CLOCK = [T0]
time.time = lambda: CLOCK[0]
time.sleep = lambda s: CLOCK.__setitem__(0, CLOCK[0] + max(0.0, s))

from plutus import config, db  # noqa: E402
from plutus.analyze import ledger as L  # noqa: E402
from plutus.sources import gecko, gmgn  # noqa: E402
from plutus.track import trackers as T  # noqa: E402
from plutus.web import app  # noqa: E402

CHAIN = "robinhood"
TOKEN = "0x" + "7" * 40
POOL = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
STAKE = "0x" + "5" * 40
DEAD = "0x000000000000000000000000000000000000dead"
ZERO_ADDR = "0x" + "0" * 40
FEE_SINK = "0x" + "fe" * 20
PID = "0x" + "e" * 64
SUPPLY = 1_000_000_000.0
FEE = 0.04
N_OURS, N_THIRD = 458, 300
DAY, HOUR = 86_400, 3_600


class StopSim(BaseException):
    """BaseException, so the loop's `except Exception` cannot swallow the end of the week."""


class RestartSim(BaseException):
    pass


# ═══════════════════════════════════════════════════════════════════ the simulated chain
class World:
    def __init__(self, seed=7, token=None, pid=None, stake=None, n_ours=None, n_third=None,
                 ours_base=0x1000, third_base=0x900000):
        self.token, self.pid, self.stake = token or TOKEN, pid or PID, stake or STAKE
        n_ours, n_third = n_ours or N_OURS, n_third or N_THIRD
        self.rng = random.Random(seed)
        self.bal: dict[str, float] = {}
        self.changed: dict[str, int] = {}
        self.trades: list[dict] = []
        self.ours = ["0x%040x" % (ours_base + i) for i in range(n_ours)]
        self.third = ["0x%040x" % (third_base + i) for i in range(n_third)]
        self.R, self.Q = 140_000_000.0, 12_000.0
        self.bal[POOL] = self.R
        for w in self.ours:
            self.bal[w] = 1_000_000.0
        self.bal[self.stake] = 50_000_000.0
        self.bal[DEAD] = 1_000_000.0
        rest = SUPPLY - sum(self.bal.values())
        weights = [self.rng.random() ** 3 for _ in self.third]
        for w, x in zip(self.third, weights):
            self.bal[w] = rest * x / sum(weights)
        for a in self.bal:
            self.changed[a] = self.block() - 1000
        # Every balance change as the chain records it: a Transfer event. Minted at launch; every
        # buy, sell, transfer and stake after. Amounts are raw integers so the log conserves
        # supply exactly, as a real token's does.
        self.xfers: list[dict] = []
        self.minted_raw = 0
        for a, v in list(self.bal.items()):
            self._xfer(ZERO_ADDR, a, v, block=self.block() - 1000)
        self.rate_limited_until = 0.0
        self.rate_limit_p = 0.0
        self.errors_p = 0.002
        self.n = 0

    def block(self) -> int:
        return 70_000_000 + int(CLOCK[0] - T0)

    def _xfer(self, frm, to, amount, block=None):
        raw = int(round(amount * 10 ** 18))
        if frm == ZERO_ADDR:
            self.minted_raw += raw
        self.xfers.append({"block": block if block is not None else self.block(),
                           "log_index": len(self.xfers), "tx_hash": f"0x{len(self.xfers):064x}",
                           "from": frm, "to": to, "raw": raw, "ts": int(CLOCK[0])})

    def _touch(self, *addrs):
        b = self.block()
        for a in addrs:
            self.changed[a] = b

    def _tape(self, maker, side, usd, tokens):
        self.n += 1
        self.trades.append({"tx_hash": f"0x{self.n:064x}", "ts": int(CLOCK[0]), "side": side,
                            "usd": usd, "maker": maker, "from_amount": usd if side == "buy" else tokens,
                            "to_amount": tokens if side == "buy" else usd, "base_amount": tokens,
                            "price_usd": usd / tokens if tokens else 0.0, "block": self.block()})

    def buy(self, maker, usd):
        k = self.R * self.Q
        q = self.Q + usd * (1 - FEE)
        out = self.R - k / q
        self.R, self.Q = self.R - out, q
        self.bal[POOL] = self.R
        self.bal[maker] = self.bal.get(maker, 0.0) + out
        self._touch(POOL, maker)
        self._xfer(POOL, maker, out)
        self._tape(maker, "buy", usd, out)

    def sell(self, maker, tokens):
        tokens = min(tokens, self.bal.get(maker, 0.0))
        if tokens <= 0:
            return
        k = self.R * self.Q
        r = self.R + tokens * (1 - FEE)
        usd = self.Q - k / r
        self.R, self.Q = r, self.Q - usd
        self.bal[POOL] = self.R
        self.bal[maker] -= tokens
        # The fee is taken in tokens and goes somewhere -- supply is conserved, as on chain.
        self.bal[FEE_SINK] = self.bal.get(FEE_SINK, 0.0) + tokens * FEE
        self._touch(POOL, maker, FEE_SINK)
        self._xfer(maker, POOL, tokens * (1 - FEE))
        self._xfer(maker, FEE_SINK, tokens * FEE)
        self._tape(maker, "sell", usd, tokens)

    def transfer(self, a, b, amount):
        """NOT on the trade feed: only a real balance read can see it."""
        amount = min(amount, self.bal.get(a, 0.0))
        self.bal[a] -= amount
        self.bal[b] = self.bal.get(b, 0.0) + amount
        self._touch(a, b)
        self._xfer(a, b, amount)

    def true_ours(self) -> float:
        return sum(self.bal[w] for w in self.ours)

    # ── what the vendor answers ────────────────────────────────────────────────────────
    def answer(self, args):
        cmd = tuple(args[:2]) if args[0] != "gas-price" else ("gas-price",)
        flags = {args[i]: args[i + 1] for i in range(len(cmd), len(args) - 1, 2)}
        if cmd == ("portfolio", "token-balance"):
            w = flags["--wallet"]
            return {"balances": [{"token_address": self.token, "balance": str(self.bal.get(w, 0.0)),
                                  "height": self.changed.get(w, self.block() - 10_000)}]}
        if cmd == ("token", "pool"):
            return {"base_reserve": self.R, "quote_reserve": self.Q, "pool_address": self.pid,
                    "liquidity": 2 * self.Q}
        if cmd == ("token", "info"):
            return {"holder_count": sum(1 for v in self.bal.values() if v > 0), "symbol": "SIM"}
        if cmd == ("token", "traders"):
            holders = [(a, v) for a, v in self.bal.items()
                       if v > 0 and a not in (POOL, self.stake, DEAD, FEE_SINK)]
            ob, tag = flags.get("--order-by"), flags.get("--tag")
            if ob == "amount_percentage":
                holders.sort(key=lambda x: -x[1])
            else:                                         # other rankings: a different slice
                seed = hash((ob, tag)) & 0xFFFF
                holders.sort(key=lambda x: hash((x[0], seed)))
            if tag:
                holders = [h for h in holders if hash((h[0], tag)) % 4 == 0]
            return {"list": [{"address": a, "balance": v, "amount_percentage": v / SUPPLY * 100,
                              "addr_type": 0} for a, v in holders[:100]]}
        raise AssertionError(f"unexpected vendor call {args}")

    def gecko_trades(self):
        return list(reversed(self.trades[-gecko.TRADES_WINDOW:]))

    # ── what Etherscan answers ─────────────────────────────────────────────────────────
    def etherscan_logs(self, from_block, to_block):
        return [dict(x) for x in self.xfers if from_block <= x["block"] <= to_block]

    def gecko_pool(self):
        return {"base_reserve": self.R, "quote_reserve": self.Q, "spot": self.Q / self.R,
                "reserve_usd": 2 * self.Q}


# ═══════════════════════════════════════════════════════════════════ the harness
class Harness:
    def __init__(self):
        self.w = World()
        self.calls = defaultdict(Counter)          # hour -> tier -> calls
        self.refused = defaultdict(Counter)
        self.log = []
        self.hourly = []
        self.events: list[tuple[float, str, object]] = []
        self.next_hour = T0 + HOUR
        self.tid = None

    # vendor + free sources
    def install(self):
        w, h = self.w, self

        def http(args, background=False):
            gmgn._pace(background)                     # the real pacer and the real budget
            if CLOCK[0] < w.rate_limited_until or w.rng.random() < w.rate_limit_p:
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

        gmgn._http_call = http
        gmgn._reserve_slot = counted
        gmgn.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "sim cli error")
        def trades(net, pool, quick=False):
            h.last_tape = CLOCK[0]
            return w.gecko_trades()
        gecko.trades = trades
        gecko.pool = lambda net, pid: w.gecko_pool()

        class Grab(logging.Handler):
            def emit(self, rec):
                if rec.levelno >= logging.WARNING:
                    h.log.append((CLOCK[0], rec.levelname, rec.getMessage()[:200]))
        logging.getLogger().addHandler(Grab())
        for name in list(logging.Logger.manager.loggerDict):
            lg = logging.getLogger(name)
            lg.addHandler(Grab()) if lg.handlers else None

    def hour(self) -> int:
        return int((CLOCK[0] - T0) // HOUR)

    # ── the week's script ──────────────────────────────────────────────────────────────
    def script(self):
        w, rng = self.w, self.w.rng
        t = T0
        while t < T0 + 7 * DAY:                        # third-party trading all week
            t += rng.expovariate(1 / 300)
            m = rng.choice(w.third)
            self.at(t, "3p", (lambda m=m: w.buy(m, rng.uniform(10, 400))) if rng.random() < 0.55
                    else (lambda m=m: w.sell(m, w.bal.get(m, 0) * rng.uniform(0.05, 0.4))))
        for i, o in enumerate(w.ours):                 # 458-wallet buy wave inside 90 seconds
            self.at(T0 + DAY + 12 * HOUR + i * 90 / N_OURS, "buy_wave", lambda o=o: w.buy(o, 20.0))
        for i in range(20):                            # transfers between our own wallets
            a, b = w.ours[i], w.ours[-1 - i]
            self.at(T0 + 2 * DAY + 6 * HOUR + i, "transfer", lambda a=a, b=b: w.transfer(a, b, w.bal[a] * 0.5))
        self.at(T0 + 2 * DAY + 18 * HOUR, "restart", "restart")
        self.at(T0 + 3 * DAY + 9 * HOUR, "429s", lambda: setattr(w, "rate_limit_p", 0.10))
        self.at(T0 + 3 * DAY + 10 * HOUR, "429s end", lambda: setattr(w, "rate_limit_p", 0.0))
        for hr in range(6):                            # another process burning the same key
            for k in range(400):
                self.at(T0 + 3 * DAY + (14 + hr) * HOUR + k * 9, "apollo", self._external_call)
        for i in range(50):                            # our wallets staking 30%
            o = w.ours[100 + i]
            self.at(T0 + 4 * DAY + 10 * HOUR + i * 5, "stake", lambda o=o: w.transfer(o, STAKE, w.bal[o] * 0.3))
        for i in range(100):                           # sell wave over 30 minutes
            o = w.ours[200 + i]
            self.at(T0 + 5 * DAY + 8 * HOUR + i * 18, "sell_wave", lambda o=o: w.sell(o, w.bal[o] * 0.2))
        self.at(T0 + 5 * DAY + 20 * HOUR, "restart", "restart")
        self.at(T0 + 6 * DAY + 12 * HOUR, "full_pull", self._full_pull)
        self.at(T0 + 6 * DAY + 15 * HOUR, "whale", lambda: w.buy(w.third[0], 8_000.0))
        self.events.sort(key=lambda e: e[0])

    def at(self, ts, kind, fn):
        self.events.append((ts, kind, fn))

    def _external_call(self):
        try:
            gmgn._reserve_slot(False)
        except gmgn.BudgetExceeded:
            pass

    def _full_pull(self):
        r = T.track_inventory(self.tid, full=True, fresh=True)
        self.full_pull_result = r

    # ── advancing time ─────────────────────────────────────────────────────────────────
    def advance(self, seconds):
        end = CLOCK[0] + seconds
        while self.events and self.events[0][0] <= end:
            ts, kind, fn = self.events.pop(0)
            CLOCK[0] = max(CLOCK[0], ts)
            if fn == "restart":
                raise RestartSim()
            fn()
        CLOCK[0] = max(CLOCK[0], end)
        while CLOCK[0] >= self.next_hour:
            self.snapshot()
            self.next_hour += HOUR
        if CLOCK[0] >= T0 + 7 * DAY:
            raise StopSim()

    def snapshot(self):
        tid, w = self.tid, self.w
        led = L.build(tid)
        rows = db.latest_balance_rows(tid)
        state = {a: (rows[a][1], rows[a][2]) for a in w.ours if a in rows}
        rolled = db.fills_after(tid, state)
        per = [abs(rows[a][0] + rolled.get(a, (0, 0))[0] - w.bal[a]) for a in w.ours if a in rows]
        stale = max((CLOCK[0] - rows[a][2]) for a in w.ours if a in rows) if rows else None
        # FAIR TRUTH: the chain as of the desk's last read of the trade feed. The desk cannot
        # know about a fill it has not polled yet, and a snapshot taken seconds into a buy wave
        # would otherwise score that as an error.
        truth = w.true_ours()
        seen_until = getattr(self, "last_tape", CLOCK[0])
        ours_set = set(w.ours)
        for tr in reversed(w.trades):
            if tr["ts"] <= seen_until:
                break
            if tr["maker"] in ours_set:
                truth -= tr["base_amount"] if tr["side"] == "buy" else -tr["base_amount"]
        self.hourly.append({"hour": self.hour(), "ours": led.ours, "truth": truth,
                            "err": abs(led.ours - truth) / truth, "wallet_err": max(per or [0]),
                            "stale_h": (stale or 0) / HOUR, "notes": list(led.notes)})

    # ── setup, as onboarding leaves it ─────────────────────────────────────────────────
    def onboard(self):
        tid = db.upsert_token(CHAIN, TOKEN, symbol="SIM", supply_nominal=SUPPLY, primary_venue=PID)
        for a in self.w.ours:
            db.classify(tid, a, "ours", source="operator")
        db.classify(tid, POOL, "pool", source="operator")
        db.classify(tid, STAKE, "locked", source="operator")
        db.classify(tid, DEAD, "burnt", source="operator")
        T.clear_abort(tid)
        errs, self.w.errors_p = self.w.errors_p, 0.0     # a clean start; errors resume after
        T.track_tape(tid)
        r = T.track_inventory(tid, full=True)
        T.track_pool(tid, True)                 # as app._onboard_scan now does
        T.track_census(tid)
        self.w.errors_p = errs
        assert r.ok, f"onboarding read failed: {r.detail}"
        self.tid = tid

    def run(self):
        h = self

        async def fake_sleep(s):
            h.advance(s)
            await _real_sleep(0)

        async def inline(fn, /, *a, **k):
            return fn(*a, **k)

        _real_sleep = asyncio.sleep
        asyncio.sleep, asyncio.to_thread = fake_sleep, inline
        self.restarts = 0
        try:
            while True:
                app._jobs.clear(); app._scans.clear(); app._loops.clear()
                try:
                    asyncio.run(app._loop(self.tid))
                except RestartSim:
                    self.restarts += 1
                    continue
                except StopSim:
                    break
        finally:
            asyncio.sleep = _real_sleep


def _report(h):
    bg = [h.calls[x]["bg"] for x in range(168)]
    op = [h.calls[x]["op"] for x in range(168)]
    worst = max(h.hourly, key=lambda s: s["err"])
    per_day = Counter(int((ts - T0) // DAY) for ts, _l, _m in h.log)
    lines = [
        f"  background calls/hour: steady median {sorted(bg[13:])[len(bg[13:]) // 2]}, max {max(bg)}"
        f" (ceiling {int(900 * 0.5)})",
        f"  operator refusals: {sum(h.refused[x]['op'] for x in h.refused)}  ·  "
        f"background refusals: {sum(h.refused[x]['bg'] for x in h.refused)}",
        f"  position error vs chain: worst {worst['err']:.3%} at hour {worst['hour']}",
        f"  most out-of-date wallet: {max(s['stale_h'] for s in h.hourly[13:]):.1f}h",
        f"  warnings per day: {dict(sorted(per_day.items()))}",
        f"  restarts survived: {h.restarts}",
        "  table rows: " + ", ".join(
            f"{t}={db.connect().execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}"
            for t in ("trades", "balances", "census", "census_meta", "pool_obs", "ticks")),
    ]
    return "\n".join(lines)


def run_week():
    h = Harness()
    h.install()
    h.onboard()
    h.script()
    h.run()
    return h


def test_a_week_of_running():
    h = run_week()
    print(_report(h))
    bg = [h.calls[x]["bg"] for x in range(168)]
    steady = sorted(bg[13:])

    # Budget: background stays well under target in steady state and never past its ceiling,
    # and nothing the operator does is ever refused.
    assert steady[len(steady) // 2] < 150, f"steady background median {steady[len(steady) // 2]}/h"
    assert max(bg) <= 450, f"background reached {max(bg)} calls in an hour (ceiling 450)"
    assert sum(h.refused[x]["op"] for x in h.refused) == 0, "an operator action was refused"

    # Accuracy: the position may be off only briefly after movements the trade feed cannot
    # see (transfers, staking), and never by much.
    worst = max(s["err"] for s in h.hourly)
    assert worst < 0.05, f"position error reached {worst:.2%}"
    run = longest = 0
    for s in h.hourly:
        run = run + 1 if s["err"] > 0.001 else 0
        longest = max(longest, run)
    assert longest <= 2, f"the position stayed off for {longest} hours in a row"

    # Freshness: every wallet within its limit -- or, while the budget is taken elsewhere, the
    # page SAYS it is overdue.
    limit_h = T.RECONCILE_S / 3600 + 0.2
    for s in h.hourly[13:]:
        if s["stale_h"] > limit_h:
            assert any("past their" in n for n in s["notes"]), (
                f"hour {s['hour']}: wallets {s['stale_h']:.1f}h old and the page did not say so")

    # Quiet, and nothing crashed.
    per_day = Counter(int((ts - T0) // DAY) for ts, _l, _m in h.log)
    assert max(per_day.values(), default=0) <= 10, f"warnings per day: {dict(per_day)}"
    errors = [m for _t, lvl, m in h.log if lvl in ("ERROR", "CRITICAL")]
    assert not errors, f"errors logged: {errors[:3]}"
    assert h.restarts == 2

    # Bounded storage.
    rows = {t: db.connect().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("census", "pool_obs", "census_meta")}
    assert rows["census"] <= 2 * 1000, f"census kept {rows['census']} rows"
    assert rows["pool_obs"] <= 7 * 1440 + 200, f"pool_obs kept {rows['pool_obs']} rows"


if __name__ == "__main__":
    try:
        test_a_week_of_running()
        print()
        print("ALL PASS")
    except AssertionError as exc:
        print()
        print(f"FAIL  {exc}")
        sys.exit(1)
