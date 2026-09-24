"""The three intents, costed. This is what the "what do you want?" box calls.

Nothing here executes. Every function returns a plan with its arithmetic exposed, because advice
you cannot audit is advice you will eventually stop reading.

ONE BOUNDARY IS ENFORCED THROUGHOUT: the curve model is only trusted up to the size its own
calibration says it is accurate to (`Calibration.accurate_to`). Above that, the plan is marked
`needs_quote` and the caller must price it from a live `order quote` instead of extrapolating a
model that was never that good out there.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from plutus.analyze.curve import Pool
from plutus.analyze.ledger import Ledger

# Break-even follow-through for a markup→distribute round trip is 2 fees compounded. At a
# measured ~5% that is ~0.31x. Below it the cycle loses money and you were the exit liquidity.
DEFAULT_FOLLOW_THROUGH_FLOOR = 0.6


def _at_price(p: Pool, price: float) -> Pool:
    """The same pool moved to `price` along its own constant-product curve. k is conserved:
    R' = sqrt(k/P'), Q' = k/R'. Used to model a retrace without inventing liquidity."""
    if price <= 0:
        return p
    r = math.sqrt(p.k / price)
    return Pool(r, p.k / r, p.fee)


@dataclass
class Path:
    name: str
    cost: float
    avg_price_x: float          # average price paid, as a multiple of spot
    end_price_x: float          # where spot finishes
    days: float | None
    note: str
    recommended: bool = False


@dataclass
class Advice:
    intent: str
    headline: str
    detail: str
    paths: list[Path] = field(default_factory=list)
    numbers: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    feasible: bool = True
    needs_quote: bool = False


# ── MODE A · acquire a supply share ───────────────────────────────────────────
def acquire(pool: Pool, led: Ledger, target_share: float, budget: float,
            sell_tokens_per_day: float = 0.0, capture: float = 0.6,
            accurate_to: float = math.inf) -> Advice:
    need = led.needed_for(target_share)
    spot = pool.spot

    if need <= 0:
        return Advice("acquire", f"Already at {led.ours_share:.2%} of effective supply.",
                      "Nothing to acquire for this target.", numbers={"need": 0})

    ok, route = led.reachable(target_share)
    if not ok:
        return Advice(
            "acquire",
            f"IMPOSSIBLE — {target_share:.1%} needs {need:,.0f} tokens.",
            f"That is more than exists. Only burnt supply is genuinely gone; everything else "
            f"can be reached at some price.",
            feasible=False, numbers={"need": need, "float": led.float_})

    if route != "float":
        # NOT a refusal. The float runs out, but the pool will sell you its inventory and
        # staked tokens can unstake and become float — both at a cost, neither impossible.
        extra = target_share * led.effective - led.ours - led.float_
        pool_frac = min(1.0, extra / led.pool) if led.pool else 1.0
        pool_cost = pool.cost_to_buy(min(extra, led.pool * 0.99))
        adv = Advice(
            "acquire",
            f"BEYOND THE FLOAT — {target_share:.1%} needs {need:,.0f} tokens, "
            f"{extra:,.0f} more than the entire float holds.",
            f"The float alone tops out at {led.float_ceiling:.2%}. Past that the tokens have to "
            f"come from the pool or from stakers unwinding, and the first of those is priced: "
            f"taking {pool_frac:.0%} of the pool's inventory costs about "
            f"${pool_cost:,.0f} and moves spot to roughly "
            f"{pool.after_buy(pool_cost).spot / spot:.1f}x. Staking "
            f"({led.locked / led.effective:.2%}) cannot be bought at all — it can only be "
            f"waited for.",
            feasible=True,
            numbers={"need": need, "extra_beyond_float": extra,
                     "float_ceiling": led.float_ceiling, "pool_cost": pool_cost,
                     "pool_fraction": pool_frac, "if_unstaked": led.if_unstaked})
        adv.warnings.append(
            "Targets above the float ceiling are not refused, but they are a different kind of "
            "operation: you stop absorbing what is offered and start paying the curve for what "
            "is not.")
        return adv

    # (A) take it off the pool now
    pool_cost = pool.cost_to_buy(need)
    pool_end = pool.after_buy(pool_cost).spot if math.isfinite(pool_cost) else math.inf

    # (B) absorb at roughly spot — the fee is the whole cost when supply comes to you
    absorb = need * spot * (1 + pool.fee)
    days = (need / (sell_tokens_per_day * capture)) if sell_tokens_per_day > 0 else None

    # (C) push and absorb the retrace, repeatedly
    tot, got, p, cycles = 0.0, 0.0, pool, 0
    while got < need and cycles < 300:
        cost = p.cost_to_push(1.15)
        got += p.tokens_from_push(1.15)
        tot += cost
        p = p.after_buy(cost)
        p = _at_price(p, p.spot * 0.92)     # let it retrace 8%; k is conserved
        cycles += 1

    paths = [
        Path("Buy it off the pool now", pool_cost,
             (pool_cost / need) / spot if math.isfinite(pool_cost) else math.inf,
             pool_end / spot if math.isfinite(pool_end) else math.inf, 0.0,
             "The benchmark, not a plan. You pay the whole path up — this is what the desk has "
             "to beat, and it is always available to you."),
        Path("Absorb sells near spot", absorb, 1 + pool.fee, 1.0, days,
             "Cheapest possible: the fee is the entire cost. Needs sellers to actually show up.",
             recommended=True),
        Path(f"Push +15%, absorb −8%, ×{cycles}", tot,
             (tot / got) / spot if got else math.inf, p.spot / spot, cycles * 0.5,
             "Your cycle strategy, costed. Dearer than pure absorption, and it leaves the price "
             "materially higher — which you may want anyway."),
    ]

    cheapest = min(paths[1].cost, paths[2].cost)
    adv = Advice(
        "acquire",
        f"Need {need:,.0f} tokens — {need/led.float_:.1%} of the float.",
        f"Absorbing is {pool_cost/absorb:.1f}x cheaper than taking it off the pool. "
        f"The spread between the patient and impatient path is "
        f"${abs(pool_cost - absorb):,.0f} on this job.",
        paths=paths,
        numbers={"need": need, "float_pct": need / led.float_ if led.float_ else None,
                 "spot": spot, "budget": budget, "cheapest": cheapest})

    if cheapest > budget:
        adv.warnings.append(
            f"Even the cheapest path (${cheapest:,.0f}) exceeds your ${budget:,.0f} budget. "
            f"Lower the target or raise the budget.")
    if need * spot > accurate_to:
        adv.needs_quote = True
        adv.warnings.append(
            f"This size is past the curve model's accuracy envelope (${accurate_to:,.0f}). "
            f"Costs shown are indicative — the executor must price each rung from a live quote.")
    return adv


# ── MODE B · push FDV to a level in a window ──────────────────────────────────
def push(pool: Pool, supply_nominal: float, target_fdv: float, minutes: int,
         max_impact: float = 0.04, accurate_to: float = math.inf) -> Advice:
    fdv0 = pool.fdv(supply_nominal)
    if target_fdv <= fdv0:
        return Advice("push", f"Already at ${fdv0:,.0f} FDV.",
                      "Target is at or below the current level.", feasible=False)

    m = target_fdv / fdv0
    cost = pool.cost_to_push(m)
    tokens = pool.tokens_from_push(m)
    slices = max(1, round(minutes / 10))
    per_slice = cost / slices

    adv = Advice(
        "push",
        f"A {m:.2f}x move costs ${cost:,.0f} and hands you {tokens:,.0f} tokens.",
        f"That is {tokens/supply_nominal:.2%} of supply at {(cost/tokens)/pool.spot:.3f}x spot. "
        f"**This is acquisition, not spend** — you were going to buy those tokens anyway; doing "
        f"it in {minutes} minutes instead of over days is what makes the chart move, and it is "
        f"graded on the same efficiency metric as any other buy.",
        numbers={"multiple": m, "cost": cost, "tokens": tokens, "fdv_from": fdv0,
                 "fdv_to": target_fdv, "slices": slices, "per_slice": per_slice,
                 "avg_price_x": (cost / tokens) / pool.spot if tokens else None})

    adv.warnings.append(
        f"Worked as {slices} slices of ${per_slice:,.0f}, re-priced each tick against live Q. "
        f"If sells arrive mid-window the same target costs LESS and the executor spends less — "
        f"it never blindly spends the headline number.")

    slice_impact = pool.price_after_buy(per_slice) / pool.spot - 1
    if slice_impact > max_impact:
        adv.warnings.append(
            f"Each slice moves price {slice_impact:.1%}, over your {max_impact:.0%} cap. "
            f"Lengthen the window or raise the cap.")
    if per_slice > accurate_to:
        adv.needs_quote = True
        adv.warnings.append(
            f"Slice size is past the model's accuracy envelope (${accurate_to:,.0f}) — "
            f"price each slice from a live quote.")
    return adv


# ── MODE C · event: sell into the catalyst, bid the retrace ───────────────────
def breakeven_follow_through(pool: Pool, push_usd: float) -> float:
    """Smallest outside-buying-per-$1-of-our-push at which a markup→distribute cycle breaks even.

    NOT an analytic constant. The naive "a round trip pays the fee twice, so you need
    1/(1-f)² - 1" computes the required PRICE RISE (~10% at a ~5% fee), which is a different
    quantity from the follow-through RATIO — how much price a dollar of outside buying produces
    depends on the curve and on how big our own push was relative to Q. Conflating the two
    understates the bar by roughly 3x.

    So it is simulated: we push, they follow, we sell back everything the push bought.
    """
    if push_usd <= 0:
        return 0.0

    def pnl(ft: float) -> float:
        p = pool
        tokens = p.buy(push_usd)
        p = p.after_buy(push_usd)
        if ft > 0:
            p = p.after_buy(push_usd * ft)
        return p.sell(tokens) - push_usd

    if pnl(0.0) >= 0:
        return 0.0
    lo, hi = 0.0, 1.0
    while pnl(hi) < 0 and hi < 1_000:
        hi *= 2
    for _ in range(60):
        mid = (lo + hi) / 2
        if pnl(mid) < 0:
            lo = mid
        else:
            hi = mid
    return hi


def event(pool: Pool, expected_inflow: float, max_drawdown: float,
          follow_through_floor: float = DEFAULT_FOLLOW_THROUGH_FLOOR,
          our_push: float = 0.0) -> Advice:
    base_out = pool.extract_for_drawdown(max_drawdown)
    after = pool.after_buy(expected_inflow) if expected_inflow > 0 else pool
    boosted = after.extract_for_drawdown(max_drawdown)

    breakeven = breakeven_follow_through(pool, our_push or expected_inflow or pool.Q * 0.1)
    ft = (expected_inflow / our_push) if our_push > 0 else None

    adv = Advice(
        "event",
        f"An inflow of ${expected_inflow:,.0f} lifts Q to ${after.Q:,.0f} and lets you "
        f"distribute ${boosted:,.0f} at a {max_drawdown:.0%} drawdown.",
        f"Without their buying you could only move ${base_out:,.0f} — the wave adds "
        f"${boosted-base_out:,.0f} of capacity "
        f"({(boosted/base_out-1) if base_out else 0:.0%} more). **Sell capacity is proportional "
        f"to Q, and their buying is what raises Q**, so the engine never guesses the top: it "
        f"recomputes the exit off live Q every tick and feeds only what the pool absorbs inside "
        f"your limit. When net third-party flow turns, it cancels the asks and places the bid "
        f"ladder — that is the retrace.",
        numbers={"base_out": base_out, "boosted_out": boosted, "q_before": pool.Q,
                 "q_after": after.Q, "spot_after": after.spot,
                 "breakeven_ft": breakeven, "follow_through": ft,
                 "tokens_out": after.tokens_for_drawdown(max_drawdown)})

    adv.warnings.append(
        f"Break-even follow-through at the measured {pool.fee:.2%} fee is {breakeven:.2f}x — "
        f"a round trip pays the fee twice. Your floor is set to {follow_through_floor:.2f}x.")
    if breakeven > follow_through_floor:
        adv.warnings.append(
            f"YOUR FLOOR IS BELOW BREAK-EVEN. At this pool depth and push size the cycle needs "
            f"{breakeven:.2f}x just to return your money, but the floor is set to "
            f"{follow_through_floor:.2f}x — so the engine would green-light events that lose. "
            f"Raise the floor above {breakeven:.2f}x, or push a larger size: the break-even bar "
            f"FALLS as the push grows, because a bigger push leaves a deeper pool to sell into.")
    if ft is not None and ft < max(follow_through_floor, breakeven):
        adv.feasible = False
        adv.warnings.append(
            f"Expected follow-through {ft:.2f}x is below your floor — the engine would refuse "
            f"to feed, because at that ratio you are the exit liquidity.")
    return adv
