"""A campaign: an intent that runs over time, watched live.

An answer computed once is a snapshot of a market that has already moved. A push to a price
target over a hundred minutes is not one decision, it is a hundred minutes of decisions, and the
only useful thing a tool can do is keep answering "what now" as the facts change.

THE POINT OF WATCHING RATHER THAN PLANNING: the plan says what it costs if you move the price
alone. You rarely do. If organic buying arrives during the window the target gets cheaper and
the right move is to spend LESS, and a static plan cannot tell you that — it just keeps handing
you the number it computed before anything happened.

State is deliberately thin. Progress is derived from observations we already store (our own fills
in the tape, our balances, the pool), never from a running total the process keeps in memory,
because a restart would silently reset it and the campaign would double-spend its budget.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from plutus import db
from plutus.analyze import distribute as D
from plutus.analyze import flow as F
from plutus.analyze import ledger as L
from plutus.analyze.curve import Pool

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY,
  token_id INTEGER NOT NULL,
  kind TEXT NOT NULL,              -- acquire | push | distribute
  params TEXT NOT NULL,            -- json, exactly what the operator chose
  started_ts INTEGER NOT NULL,
  deadline_ts INTEGER,             -- push only
  state TEXT NOT NULL DEFAULT 'running',   -- running | done | stopped
  baseline TEXT                    -- json snapshot at start, so progress is measurable
);
"""


def ensure() -> None:
    db.connect().executescript(SCHEMA)
    db.connect().commit()


@dataclass
class Step:
    """What to do right now, and why."""
    action: str                  # BUY | SELL | HOLD | DONE | STOP
    usd: float = 0.0
    tokens: float = 0.0
    reason: str = ""
    urgency: str = "normal"      # normal | behind | ahead


@dataclass
class Status:
    id: int
    kind: str
    state: str
    started_ts: int
    deadline_ts: int | None
    elapsed_s: int
    remaining_s: int | None
    time_pct: float | None       # how far through the window
    progress_pct: float          # how far to the goal
    params: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    participants: dict = field(default_factory=dict)
    now: dict = field(default_factory=dict)
    done: dict = field(default_factory=dict)     # what WE have actually done
    step: Step = field(default_factory=lambda: Step("HOLD"))
    notes: list[str] = field(default_factory=list)


def create(token_id: int, kind: str, params: dict, pool: Pool, led: L.Ledger,
           minutes: int | None = None) -> int:
    ensure()
    now = db.now()
    baseline = {"spot": pool.spot, "fdv": pool.fdv(led.nominal), "Q": pool.Q, "R": pool.R,
                "ours": led.ours, "ours_share": led.ours_share, "float": led.float_}
    cur = db.connect().execute(
        "INSERT INTO campaigns (token_id,kind,params,started_ts,deadline_ts,state,baseline) "
        "VALUES (?,?,?,?,?,'running',?)",
        (token_id, kind, json.dumps(params), now,
         (now + minutes * 60) if minutes else None, json.dumps(baseline)))
    db.connect().commit()
    return cur.lastrowid


def get(cid: int) -> dict | None:
    ensure()
    r = db.connect().execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def listing(token_id: int | None = None) -> list[dict]:
    ensure()
    q = "SELECT * FROM campaigns"
    a: list = []
    if token_id:
        q += " WHERE token_id=?"
        a.append(token_id)
    return [dict(r) for r in db.connect().execute(q + " ORDER BY started_ts DESC LIMIT 50", a)]


def stop(cid: int, state: str = "stopped") -> None:
    db.connect().execute("UPDATE campaigns SET state=? WHERE id=?", (state, cid))
    db.connect().commit()


def _our_fills(token_id: int, since: int) -> dict:
    """What WE have actually done, from the tape — not from a counter in memory."""
    r = db.connect().execute(
        """SELECT
             COALESCE(SUM(CASE WHEN side='buy'  THEN usd END),0)    bought_usd,
             COALESCE(SUM(CASE WHEN side='buy'  THEN tokens END),0) bought_tok,
             COALESCE(SUM(CASE WHEN side='sell' THEN usd END),0)    sold_usd,
             COALESCE(SUM(CASE WHEN side='sell' THEN tokens END),0) sold_tok,
             COUNT(*) fills
           FROM trades WHERE token_id=? AND is_ours=1 AND ts>=?""",
        (token_id, since)).fetchone()
    d = dict(r)
    d["avg_buy"] = (d["bought_usd"] / d["bought_tok"]) if d["bought_tok"] else None
    d["avg_sell"] = (d["sold_usd"] / d["sold_tok"]) if d["sold_tok"] else None
    return d


def participants(token_id: int, since: int) -> dict:
    """Every wallet that traded during the campaign, and the us-versus-them split.

    A campaign is not just a price path, it is a contest over who is on which side of it. The
    number that decides whether a push was worth running is not how far the chart moved but how
    much of the volume was OUR OWN money — a move carried entirely by us is a move that unwinds
    the moment we stop, and that is invisible unless the split is kept.
    """
    rows = db.connect().execute(
        """SELECT maker, is_ours,
                  COALESCE(SUM(CASE WHEN side='buy'  THEN usd END),0) buy_usd,
                  COALESCE(SUM(CASE WHEN side='sell' THEN usd END),0) sell_usd,
                  COALESCE(SUM(CASE WHEN side='buy'  THEN tokens END),0) buy_tok,
                  COALESCE(SUM(CASE WHEN side='sell' THEN tokens END),0) sell_tok,
                  COUNT(*) fills, MAX(ts) last_ts
           FROM trades WHERE token_id=? AND ts>=? AND maker<>''
           GROUP BY maker, is_ours ORDER BY (buy_usd+sell_usd) DESC""",
        (token_id, since)).fetchall()
    wallets = []
    for r in rows:
        d = dict(r)
        d["volume"] = d["buy_usd"] + d["sell_usd"]
        d["net"] = d["buy_usd"] - d["sell_usd"]
        wallets.append(d)

    def tot(pred):
        sel = [w for w in wallets if pred(w)]
        return {
            "wallets": len(sel),
            "fills": sum(w["fills"] for w in sel),
            "buy_usd": sum(w["buy_usd"] for w in sel),
            "sell_usd": sum(w["sell_usd"] for w in sel),
            "buy_tok": sum(w["buy_tok"] for w in sel),
            "sell_tok": sum(w["sell_tok"] for w in sel),
            "volume": sum(w["volume"] for w in sel),
            "net": sum(w["net"] for w in sel),
        }

    ours, third = tot(lambda w: w["is_ours"]), tot(lambda w: not w["is_ours"])
    total_vol = ours["volume"] + third["volume"]
    return {
        "wallets": wallets[:60],
        "ours": ours, "third": third,
        "total_volume": total_vol,
        "our_share_of_volume": (ours["volume"] / total_vol) if total_vol else 0.0,
        "active": len(wallets),
    }


def status(cid: int, pool: Pool, led: L.Ledger) -> Status | None:
    c = get(cid)
    if not c:
        return None
    p = json.loads(c["params"] or "{}")
    b = json.loads(c["baseline"] or "{}")
    now = db.now()
    elapsed = now - c["started_ts"]
    remaining = (c["deadline_ts"] - now) if c["deadline_ts"] else None
    total = (c["deadline_ts"] - c["started_ts"]) if c["deadline_ts"] else None
    time_pct = min(1.0, elapsed / total) if total else None

    done = _our_fills(c["token_id"], c["started_ts"])
    fl = F.measure(c["token_id"], 900)
    parts = participants(c["token_id"], c["started_ts"])
    st = Status(id=cid, kind=c["kind"], state=c["state"], started_ts=c["started_ts"],
                deadline_ts=c["deadline_ts"], elapsed_s=elapsed, remaining_s=remaining,
                time_pct=time_pct, progress_pct=0.0, params=p, baseline=b, done=done,
                now={"spot": pool.spot, "fdv": pool.fdv(led.nominal), "Q": pool.Q,
                     "ours": led.ours, "ours_share": led.ours_share,
                     "third_net_15m": fl.third_net, "regime": F.regime(fl)[0]})
    st.participants = parts

    # The final read on a push: what fraction of the move did we pay for ourselves?
    if b.get("spot") and pool.spot:
        st.now["price_move_x"] = pool.spot / b["spot"]

    if c["kind"] == "push":
        _push(st, p, b, pool, led, fl)
    elif c["kind"] == "acquire":
        _acquire(st, p, b, pool, led, fl)
    elif c["kind"] == "distribute":
        _distribute(st, p, b, pool, led, fl)
    # Advice built on holders must say when the census under it is partial.
    st.notes.extend(n for n in L.census_notes(c["token_id"]) if n.startswith("CENSUS PARTIAL"))
    return st


# ── push ──────────────────────────────────────────────────────────────────────
def _push(st: Status, p: dict, b: dict, pool: Pool, led: L.Ledger, fl: F.Flow) -> None:
    target = float(p.get("target_fdv") or 0)
    start_fdv = float(b.get("fdv") or 0)
    now_fdv = pool.fdv(led.nominal)
    if target <= start_fdv:
        st.step = Step("DONE", reason="target was at or below the starting price")
        return

    st.progress_pct = max(0.0, min(1.0, (now_fdv - start_fdv) / (target - start_fdv)))

    if now_fdv >= target:
        st.step = Step("DONE", reason=f"target reached — FDV ${now_fdv:,.0f}")
        st.notes.append(f"spent ${st.done['bought_usd']:,.0f} to get here; the plan assumed "
                        f"you would move it alone")
        return
    if st.remaining_s is not None and st.remaining_s <= 0:
        st.step = Step("STOP", reason="the window has expired")
        return

    # what is still needed, priced against the pool AS IT IS NOW — not as it was at the start
    m = target / now_fdv
    cost_left = pool.cost_to_push(m)
    slices_left = max(1, round((st.remaining_s or 600) / 600))
    per_slice = cost_left / slices_left

    # Are we ahead or behind? Compare price progress against time progress.
    if st.time_pct is not None and st.time_pct > 0.05:
        drift = st.progress_pct - st.time_pct
        if drift > 0.1:
            st.step = Step("HOLD", reason=(
                f"ahead of schedule — {st.progress_pct:.0%} of the move done in "
                f"{st.time_pct:.0%} of the time. Someone else is buying; every dollar they "
                f"spend is one you do not have to. Let it run."), urgency="ahead")
            st.notes.append(f"third-party flow last 15m: ${fl.third_net:+,.0f}")
            return
        if drift < -0.15:
            st.step = Step("BUY", usd=per_slice, tokens=pool.buy(per_slice), urgency="behind",
                           reason=(f"behind schedule — {st.progress_pct:.0%} done with "
                                   f"{st.time_pct:.0%} of the window gone. ${cost_left:,.0f} "
                                   f"still required, {slices_left} slices left."))
            return

    st.step = Step("BUY", usd=per_slice, tokens=pool.buy(per_slice),
                   reason=(f"on pace. ${cost_left:,.0f} left to the target at the CURRENT pool, "
                           f"{slices_left} slices remaining — ${per_slice:,.0f} this one."))
    if fl.third_net > per_slice:
        st.notes.append(
            f"third parties bought ${fl.third_net:,.0f} in the last 15m, more than this slice. "
            f"Consider holding and letting them carry it.")


# ── acquire ───────────────────────────────────────────────────────────────────
def _acquire(st: Status, p: dict, b: dict, pool: Pool, led: L.Ledger, fl: F.Flow) -> None:
    target_share = float(p.get("target_share") or 0)
    start = float(b.get("ours") or 0)
    need_total = max(0.0, target_share * led.effective - start)
    got = led.ours - start
    st.progress_pct = min(1.0, got / need_total) if need_total else 1.0

    if led.ours_share >= target_share:
        st.step = Step("DONE", reason=f"holding {led.ours_share:.2%} of tradeable supply")
        return

    budget = float(p.get("budget") or 0)
    spent = st.done["bought_usd"]
    if budget and spent >= budget:
        st.step = Step("STOP", reason=f"budget spent (${spent:,.0f} of ${budget:,.0f})")
        return

    regime = F.regime(fl)[0]
    if regime in ("STAND_DOWN", "FARMED"):
        st.step = Step("HOLD", reason=(
            f"regime is {regime.replace('_',' ')} — supply is not on offer, and anything taken "
            f"here is taken at the expensive end of the curve. The bid rests deep and spends "
            f"nothing."))
        return
    if fl.third_sell <= 0:
        st.step = Step("HOLD", reason="nobody is selling this window; there is nothing to absorb")
        return

    take = min(fl.third_sell, pool.extract_for_drawdown(0.04))
    st.step = Step("BUY", usd=take, tokens=pool.buy(take), reason=(
        f"${fl.third_sell:,.0f} of third-party selling arrived — absorb it. This is the cheap "
        f"supply: it comes to you at roughly market instead of being chased up the curve."))


# ── distribute ────────────────────────────────────────────────────────────────
def _distribute(st: Status, p: dict, b: dict, pool: Pool, led: L.Ledger, fl: F.Flow) -> None:
    cap = float(p.get("max_sell_tokens") or 0)
    s = D.Settings(participation=float(p.get("participation") or 0.35),
                   floor_price=float(p.get("floor_price") or 0),
                   max_sell_tokens=cap,
                   trail_from_peak=float(p.get("trail_from_peak") or 0.30))
    peak = db.connect().execute(
        "SELECT MAX(price) p FROM trades WHERE token_id=? AND ts>=? AND price>0",
        (st.id and get(st.id)["token_id"], st.started_ts)).fetchone()["p"] or pool.spot
    plan = D.decide(pool, s, inflow_usd=max(0.0, fl.third_net), price_peak=peak,
                    already_sold_tokens=st.done["sold_tok"],
                    already_sold_usd=st.done["sold_usd"])
    st.progress_pct = (st.done["sold_tok"] / cap) if cap else 0.0
    st.step = Step({"FEED": "SELL", "WAIT": "HOLD", "STOP": "STOP"}[plan.state],
                   usd=plan.sell_usd, tokens=plan.sell_tokens, reason=plan.reason)
    st.notes.extend(plan.warnings)
    st.now["peak_price"] = peak
    st.now["pool_capacity_usd"] = plan.pool_capacity_usd
