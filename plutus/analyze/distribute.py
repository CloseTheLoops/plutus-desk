"""Selling into a catalyst — without having to predict the catalyst.

THE DESIGN ERROR THIS REPLACES. The first version asked the operator for "expected outside
buying", which is the one number nobody can know in advance. Asking for it meant the answer was
only as good as the guess, and a wrong guess produced confident arithmetic on a fiction.

WHAT REPLACES IT: a PARTICIPATION RATE. You do not forecast the inflow, you decide what SHARE of
whatever arrives you are willing to take. The tool measures the inflow live, tick by tick, and
sells that share of it. The whole plan becomes a policy you set once rather than a prediction you
have to be right about.

Why it works, measured on a real pool with a $20k inflow:

    participation   you receive   price ends
        0.25          $4,730        4.24x
        0.50          $9,388        2.86x
        0.75         $13,948        1.77x
        1.00         $18,365        0.96x   <- you sold the entire rally into itself

Below 1.0 the price still rises while you distribute, because you are taking less out than is
coming in. At 1.0 you exactly cancel the buying and the move dies with your inventory still
half-sold. That is the trade-off, and it is a dial, not a forecast.

TWO TERMS THE OLD UI USED BADLY:
  "max drawdown"       — how far YOUR OWN selling pushes the price down. It is not a market
                         risk, it is self-inflicted, and at thin depth it binds brutally: on a
                         $13k pool, accepting a 15% self-drawdown yields under $1,000.
  "min follow-through" — an attempt to ask "is this catalyst real". Unanswerable in advance.
                         Replaced by a live check: only sell while third-party buying actually
                         exceeds our selling. If the demand is not there, nothing is sold.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from plutus.analyze.curve import Pool

# States the desk can be in during a distribution.
WAIT = "WAIT"       # demand has not arrived (or has stopped) — do not sell
FEED = "FEED"       # demand is arriving — sell our share of it
STOP = "STOP"       # a limit was hit: floor price, cap, or trailing stop


@dataclass
class Settings:
    """Everything the operator can actually know, and nothing they cannot."""
    participation: float = 0.35      # share of incoming buy flow we take
    floor_price: float = 0.0         # never sell below this
    max_sell_tokens: float = 0.0     # hard cap on how much leaves our hands, 0 = uncapped
    trail_from_peak: float = 0.30    # stop if price falls this far from its peak during the run
    min_inflow_usd: float = 25.0     # ignore dust; do not react to a $3 buy


@dataclass
class Plan:
    state: str
    sell_tokens: float = 0.0
    sell_usd: float = 0.0
    reason: str = ""
    inflow_usd: float = 0.0          # measured third-party net buying in the window
    price: float = 0.0
    peak_price: float = 0.0
    sold_tokens: float = 0.0
    sold_usd: float = 0.0
    avg_price: float | None = None
    remaining_cap: float | None = None
    pool_capacity_usd: float = 0.0   # what the pool absorbs at our impact limit right now
    warnings: list[str] = field(default_factory=list)


def decide(pool: Pool, s: Settings, inflow_usd: float, price_peak: float,
           already_sold_tokens: float = 0.0, already_sold_usd: float = 0.0,
           max_impact: float = 0.05) -> Plan:
    """What to do RIGHT NOW, from measured flow only.

    `inflow_usd` is third-party NET buying over the tick — ours excluded, which matters because
    our own sells would otherwise look like market activity and the desk would react to itself.
    """
    px = pool.spot
    peak = max(price_peak or 0.0, px)
    p = Plan(state=WAIT, inflow_usd=inflow_usd, price=px, peak_price=peak,
             sold_tokens=already_sold_tokens, sold_usd=already_sold_usd,
             avg_price=(already_sold_usd / already_sold_tokens) if already_sold_tokens else None,
             remaining_cap=(s.max_sell_tokens - already_sold_tokens)
             if s.max_sell_tokens else None,
             pool_capacity_usd=pool.extract_for_drawdown(max_impact))

    if s.floor_price and px < s.floor_price:
        p.state = STOP
        p.reason = (f"price {px:.3e} is below your floor {s.floor_price:.3e} — selling here "
                    f"would realise less than you said you would accept")
        return p

    if peak > 0 and px < peak * (1 - s.trail_from_peak):
        p.state = STOP
        p.reason = (f"price is {(1 - px/peak):.0%} off the peak of this run ({peak:.3e}), past "
                    f"your {s.trail_from_peak:.0%} trail — the move is over, stop feeding it")
        return p

    if s.max_sell_tokens and already_sold_tokens >= s.max_sell_tokens:
        p.state = STOP
        p.reason = f"cap reached — {already_sold_tokens:,.0f} tokens sold"
        return p

    if inflow_usd < s.min_inflow_usd:
        p.state = WAIT
        p.reason = (f"third-party buying is ${inflow_usd:,.0f} this window, under the "
                    f"${s.min_inflow_usd:,.0f} floor. Selling into no demand is just supply — "
                    f"it moves the price down and gets you nothing for it")
        return p

    # take our share of what actually arrived
    want_usd = inflow_usd * s.participation
    cap_usd = p.pool_capacity_usd
    if want_usd > cap_usd:
        p.warnings.append(
            f"our share (${want_usd:,.0f}) exceeds what the pool absorbs inside a "
            f"{max_impact:.0%} impact limit (${cap_usd:,.0f}) — trimmed to the limit")
        want_usd = cap_usd

    tokens = want_usd / px if px else 0.0
    if s.max_sell_tokens:
        tokens = min(tokens, max(0.0, s.max_sell_tokens - already_sold_tokens))

    p.state = FEED
    p.sell_tokens = tokens
    p.sell_usd = pool.sell(tokens)
    p.reason = (f"${inflow_usd:,.0f} of third-party buying arrived; taking "
                f"{s.participation:.0%} of it. Selling less than is coming in means the price "
                f"keeps rising while you distribute")
    return p


def preview(pool: Pool, s: Settings, inflows: tuple[float, ...] = (0, 2_000, 5_000, 20_000, 50_000),
            steps: int = 40) -> list[dict]:
    """What a given inflow would yield at this participation rate, before anything happens.

    Simulated as interleaved steps rather than one lump, because the order matters: their buying
    lifts the price that our selling then receives.
    """
    out = []
    for inflow in inflows:
        q, got, sold = pool, 0.0, 0.0
        for _ in range(steps if inflow else 1):
            if inflow:
                q = q.after_buy(inflow / steps)
                want = (inflow / steps) * s.participation
                tok = (want / q.spot) if q.spot else 0.0
                if s.max_sell_tokens and sold + tok > s.max_sell_tokens:
                    tok = max(0.0, s.max_sell_tokens - sold)
                if s.floor_price and q.spot < s.floor_price:
                    tok = 0.0
                got += q.sell(tok)
                sold += tok
                q = q.after_sell(tok)
        out.append({
            "inflow": inflow, "received": got, "tokens_sold": sold,
            "end_price_x": (q.spot / pool.spot) if pool.spot else 0.0,
            "avg_price_x": ((got / sold) / pool.spot) if sold and pool.spot else None,
            "rally_survives": q.spot > pool.spot,
        })
    return out


def participation_curve(pool: Pool, inflow: float, holdings: float,
                        rates: tuple[float, ...] = (0.1, 0.25, 0.35, 0.5, 0.75, 1.0),
                        steps: int = 40) -> list[dict]:
    """The dial, for one assumed inflow: what each participation rate trades away.

    This is the honest way to show the choice — not "what will the catalyst be worth", but
    "for ANY given catalyst, here is what taking more of it costs you in price".
    """
    rows = []
    for f in rates:
        q, got, sold = pool, 0.0, 0.0
        for _ in range(steps):
            q = q.after_buy(inflow / steps)
            tok = ((inflow / steps) * f / q.spot) if q.spot else 0.0
            got += q.sell(tok)
            sold += tok
            q = q.after_sell(tok)
        rows.append({
            "participation": f, "received": got, "tokens_sold": sold,
            "pct_of_holdings": (sold / holdings) if holdings else 0.0,
            "end_price_x": (q.spot / pool.spot) if pool.spot else 0.0,
            "rally_survives": q.spot > pool.spot,
        })
    return rows
