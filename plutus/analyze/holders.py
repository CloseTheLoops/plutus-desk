"""Per-wallet detail for the float — the drill-down behind the composition summary.

The summary answers "what kind of supply is out there". This answers "who, specifically, and
what are they likely to do". Same exclusion rules: every classified address (ours, pool, burnt,
locked) is removed first, and `addr_type` pool rows are dropped, so every row here is a real
third-party wallet.

THE COLUMN THAT IS NOT IN THE VENDOR DATA is recent activity. The census is a snapshot — it
tells you what someone holds, not whether they are moving. Joining it against our own trade tape
adds "bought or sold in the last N hours", which is the difference between a list of names and a
list of names worth watching. A dormant whale and an actively-selling whale hold the same tokens
and mean completely different things for a bid.

Shares are quoted against the TRUE float (the ledger residual), not against the sum of what the
census reached, so they stay comparable with everything else on the page.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from plutus import db

DAY = 86400


@dataclass
class Holder:
    address: str
    tokens: float
    share_float: float          # of the true float
    share_supply: float
    usd: float
    avg_cost: float | None
    vs_cost: float | None       # spot / avg_cost. 0.10 = down 90%
    segment: str
    entered_ts: int
    held_days: float | None
    last_active_ts: int
    dormant_days: float | None
    realized: float
    unrealized: float
    has_sold: bool
    tags: list[str]
    suspicious: bool
    fresh: bool
    transfer_in: bool
    bought_24h: float = 0.0     # from OUR tape, not the vendor
    sold_24h: float = 0.0
    fills_24h: int = 0
    cumulative_share: float = 0.0


@dataclass
class HolderView:
    holders: list[Holder] = field(default_factory=list)
    float_true: float = 0.0
    float_seen: float = 0.0
    coverage: float = 1.0
    exited: int = 0
    sweep_ts: int | None = None
    spot: float = 0.0
    supply: float = 0.0
    active_24h: int = 0
    notes: list[str] = field(default_factory=list)


def _segment(avg_cost: float, spot: float) -> str:
    if avg_cost <= 0:
        return "never bought"
    r = spot / avg_cost
    if r < 0.5:
        return "deep underwater"
    if r < 1:
        return "mild underwater"
    return "in profit"


def build(token_id: int, spot: float, float_true: float,
          supply: float, activity_window_s: int = DAY) -> HolderView:
    v = HolderView(float_true=float_true, spot=spot, supply=supply)
    sweep = db.latest_census_ts(token_id)
    v.sweep_ts = sweep
    if not sweep:
        v.notes.append("no census yet — run the census tracker")
        return v

    known = set(db.class_map(token_id))
    rows = [r for r in db.census_rows(token_id, sweep)
            if r["address"] not in known and (r["addr_type"] or 0) != 2]
    holding = [r for r in rows if (r["balance"] or 0) > 0]
    v.exited = len(rows) - len(holding)
    v.float_seen = sum(r["balance"] or 0 for r in holding)
    if not v.float_true:
        v.float_true = v.float_seen
    v.coverage = v.float_seen / v.float_true if v.float_true else 1.0

    # recent activity from OUR tape — the column the vendor snapshot cannot give us
    since = db.now() - activity_window_s
    act: dict[str, dict] = {}
    for t in db.connect().execute(
            """SELECT maker, side, COALESCE(SUM(usd),0) usd, COUNT(*) n
               FROM trades WHERE token_id=? AND ts>=? AND is_ours=0 AND maker<>''
               GROUP BY maker, side""", (token_id, since)).fetchall():
        a = act.setdefault(t["maker"], {"buy": 0.0, "sell": 0.0, "n": 0})
        a[t["side"] or "buy"] = float(t["usd"] or 0)
        a["n"] += int(t["n"] or 0)

    now = time.time()
    out: list[Holder] = []
    for r in sorted(holding, key=lambda x: -(x["balance"] or 0)):
        bal = float(r["balance"] or 0)
        ac = float(r["avg_cost"] or 0)
        t0 = int(r["start_holding_at"] or 0)
        la = int(r["last_active"] or 0)
        a = act.get(r["address"], {})
        out.append(Holder(
            address=r["address"], tokens=bal,
            share_float=bal / v.float_true if v.float_true else 0.0,
            share_supply=bal / supply if supply else 0.0,
            usd=bal * spot,
            avg_cost=ac or None,
            vs_cost=(spot / ac) if ac > 0 else None,
            segment=_segment(ac, spot),
            entered_ts=t0, held_days=((now - t0) / DAY) if t0 else None,
            last_active_ts=la, dormant_days=((now - la) / DAY) if la else None,
            realized=float(r["realized_profit"] or 0),
            unrealized=float(r["unrealized_profit"] or 0),
            has_sold=float(r["realized_profit"] or 0) != 0,
            tags=[t for t in (r["tags"] or "").split(",") if t],
            suspicious=bool(r["is_suspicious"]), fresh=bool(r["is_new"]),
            transfer_in=bool(r["transfer_in"]),
            bought_24h=a.get("buy", 0.0), sold_24h=a.get("sell", 0.0),
            fills_24h=a.get("n", 0),
        ))

    cum = 0.0
    for h in out:
        cum += h.share_float
        h.cumulative_share = cum
    v.holders = out
    v.active_24h = sum(1 for h in out if h.fills_24h)

    if v.coverage < 0.99:
        v.notes.append(
            f"the census reached {v.coverage:.1%} of the float; "
            f"{v.float_true - v.float_seen:,.0f} tokens sit in wallets below every ranked "
            f"slice's cutoff and are not listed here")
    if v.active_24h == 0 and out:
        v.notes.append("none of these wallets traded in the window — the list is holdings, "
                       "not activity")
    return v


def reachable_by(view: HolderView, need_tokens: float) -> dict:
    """How many of the largest holders it would take to cover `need_tokens`.

    This is the OTC question stated arithmetically: if the top handful of wallets hold most of
    what a target requires, the job is a set of conversations rather than a market operation —
    and conversations have no price impact.
    """
    if need_tokens <= 0:
        return {"n": 0, "tokens": 0.0, "covered": 1.0}
    run = 0.0
    for i, h in enumerate(view.holders, 1):
        run += h.tokens
        if run >= need_tokens:
            return {"n": i, "tokens": run, "covered": 1.0}
    return {"n": len(view.holders), "tokens": run,
            "covered": run / need_tokens if need_tokens else 0.0}
