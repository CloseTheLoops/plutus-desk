"""Who holds the float, and how it is likely to behave when you bid.

Computed on THIRD PARTY ONLY — every classified address (ours, pool, burnt, locked) is removed
first, and pool/AMM rows are dropped by `addr_type`. A curve holding 80% is mechanics; a wallet
holding 80% is something else entirely, and lumping them together makes every concentration
number meaningless.

Two segments do most of the work:
  never-bought   avg_cost == 0 and a positive balance -> arrived by transfer. No cost basis means
                 no loss-aversion anchor and no break-even to wait for: the softest supply there is.
  underwater     bought above spot. Stubborn today, but a markup walks price TOWARD their
                 break-even, so a rally manufactures the sell pressure it then has to absorb.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from plutus import db


@dataclass
class Segment:
    name: str
    wallets: int
    tokens: float
    share_of_float: float
    note: str


@dataclass
class Composition:
    holders: int = 0
    float_tokens: float = 0.0        # the float the CENSUS could see
    float_true: float = 0.0          # the ledger residual — the real float
    coverage: float = 1.0            # float_tokens / float_true
    segments: list[Segment] = field(default_factory=list)
    top: list[dict] = field(default_factory=list)
    concentration: dict[int, float] = field(default_factory=dict)
    exited: int = 0
    sweep_ts: int | None = None
    notes: list[str] = field(default_factory=list)


def build(token_id: int, spot: float, float_true: float = 0.0) -> Composition:
    """`float_true` is the ledger's residual. Concentration is quoted against IT, not against
    what the census happened to reach, because the two are not the same number and the gap is
    exactly the supply we cannot see."""
    sweep = db.latest_census_ts(token_id)
    c = Composition(sweep_ts=sweep, float_true=float_true)
    all_rows, complete = db.holder_rows(token_id)
    if not all_rows:
        c.notes.append("no census yet — run the census tracker")
        return c

    known = set(db.class_map(token_id))
    rows = [r for r in all_rows
            if r["address"] not in known and (r["addr_type"] or 0) != 2]
    holding = [r for r in rows if (r["balance"] or 0) > 0]
    c.exited = len(rows) - len(holding)
    c.holders = len(holding)
    c.float_tokens = sum(r["balance"] or 0 for r in holding)
    if not c.float_tokens:
        c.notes.append("census returned no third-party holders with a balance")
        return c
    if not c.float_true:
        c.float_true = c.float_tokens
    c.coverage = c.float_tokens / c.float_true if c.float_true else 1.0
    uncovered = c.float_true - c.float_tokens
    if uncovered > c.float_true * 0.01:
        c.notes.append(
            f"the census reached {c.coverage:.1%} of the float — {uncovered:,.0f} tokens "
            f"({uncovered/c.float_true:.1%}) sit in wallets below every ranked slice's cutoff. "
            f"Segment and concentration shares below are quoted against the TRUE float, so the "
            f"uncovered remainder shows up as its own row rather than being silently absorbed.")

    def seg(name: str, pred, note: str) -> None:
        sel = [r for r in holding if pred(r)]
        tok = sum(r["balance"] or 0 for r in sel)
        c.segments.append(Segment(name, len(sel), tok, tok / c.float_true, note))

    # avg_cost None means "bought, cost unknown" -- NOT never bought. Treating the two alike
    # filed every holder the census had not priced as the softest supply there is.
    seg("never bought", lambda r: r["avg_cost"] is not None and r["avg_cost"] <= 0,
        "arrived by transfer, never bought from the pool: no break-even to wait for")
    seg("cost unknown", lambda r: r["avg_cost"] is None,
        "bought from the pool, but no cost basis is known for them yet")
    seg("deep underwater", lambda r: 0 < (r["avg_cost"] or 0) and spot / r["avg_cost"] < 0.5,
        "below half their cost")
    seg("mild underwater", lambda r: 0 < (r["avg_cost"] or 0) and 0.5 <= spot / r["avg_cost"] < 1,
        "a modest rally puts them at break-even, which is when they sell")
    seg("in profit", lambda r: 0 < (r["avg_cost"] or 0) <= spot,
        "the only cohort with profit-taking pressure today")

    tag_counts: dict[str, list] = {}
    for r in holding:
        for t in (r["tags"] or "").split(","):
            if t:
                tag_counts.setdefault(t, []).append(r)
    for t, sel in sorted(tag_counts.items(), key=lambda kv: -sum(x["balance"] or 0 for x in kv[1]))[:4]:
        tok = sum(x["balance"] or 0 for x in sel)
        c.segments.append(Segment(f"tagged {t}", len(sel), tok, tok / c.float_true,
                                  "vendor tag" if t != "dex_bot" else
                                  "automated — responds to price mechanically, so learnable"))

    if uncovered > 0:
        c.segments.append(Segment("NOT REACHED by the census", 0, uncovered,
                                  uncovered / c.float_true,
                                  "below every ranked slice's cutoff — uncovered, not absent"))

    ranked = sorted(holding, key=lambda r: -(r["balance"] or 0))
    for n in (5, 10, 15, 30, 50):
        c.concentration[n] = sum(r["balance"] or 0 for r in ranked[:n]) / c.float_true
    c.top = [{
        "address": r["address"], "tokens": r["balance"] or 0,
        "share": (r["balance"] or 0) / c.float_true,
        "usd": (r["balance"] or 0) * spot,
        "avg_cost": r["avg_cost"] or 0,
        "vs_spot": (spot / r["avg_cost"]) if (r["avg_cost"] or 0) > 0 else None,
        "tags": r["tags"] or "",
    } for r in ranked[:20]]

    nb = next((s for s in c.segments if s.name == "never bought"), None)
    uw = sum(s.share_of_float for s in c.segments if "underwater" in s.name)
    if nb and nb.share_of_float > 0.25:
        c.notes.append(
            f"{nb.share_of_float:.0%} of the float has no cost basis — take that supply before "
            f"any markup leg, not after")
    if uw > 0.3:
        c.notes.append(
            f"{uw:.0%} of the float is underwater: a push walks price toward their break-even "
            f"and manufactures the sells you then absorb with your own budget")
    if c.concentration.get(50, 0) > 0.8:
        c.notes.append(
            f"the top 50 holders are {c.concentration[50]:.0%} of the float — this is an OTC "
            f"problem wearing a market-making costume, and OTC has no price impact")
    return c
