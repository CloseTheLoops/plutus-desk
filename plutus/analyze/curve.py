"""Pool arithmetic: constant product, plus the per-token execution fee that the model alone misses.

WHY THE FEE IS NOT OPTIONAL. On the first token measured, the pool's own `fee` field read 0 and
pure `x*y=k` said a $100 buy should cost 0.67%. It actually cost 5.57%. The gap is a constant
MULTIPLICATIVE fee (a v4 hook, most likely the launchpad's), and decomposing

    total_cost = 1 - (1 - fee)(1 - curve_impact)

recovered it as a tight band across a 100x size range -- under half a point of spread, i.e. genuinely constant.
Without it the model understates cost by 8x at small fill sizes, which is exactly the size a
ladder fills at.

So: the curve model is correct for IMPACT, and useless until the fee is applied. The fee is
measured per token at onboarding and stored in the database. **It is never a literal here.**

All functions take (R, Q) -- base reserve in tokens, quote reserve in the quote asset -- and a
fee as a fraction. They are pure; no I/O.

TWO IDENTITIES RUN THE WHOLE DESK, and both are linear in Q:
    cost to move price by factor m   =  Q(sqrt(m) - 1) / (1 - fee)
    proceeds for a drawdown of d     =  Q(1 - sqrt(1 - d)) * (1 - fee)
A deeper pool makes pushes cheaper AND exits bigger, at the same time. That is why Q, not price,
is the number this desk watches hardest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Pool:
    R: float                 # base token reserve
    Q: float                 # quote reserve (USD-ish if the quote is a stablecoin)
    fee: float = 0.0         # measured multiplicative execution cost, per trade

    @property
    def k(self) -> float:
        return self.R * self.Q

    @property
    def spot(self) -> float:
        return self.Q / self.R if self.R else 0.0

    def fdv(self, supply: float) -> float:
        return self.spot * supply

    # ── taking ────────────────────────────────────────────────────────────────
    def buy(self, spend: float) -> float:
        """Tokens received for `spend` of quote, fee applied to the input."""
        if spend <= 0:
            return 0.0
        net = spend * (1 - self.fee)
        return self.R - self.k / (self.Q + net)

    def sell(self, tokens: float) -> float:
        """Quote received for selling `tokens`, fee applied to the output."""
        if tokens <= 0:
            return 0.0
        gross = self.Q - self.k / (self.R + tokens)
        return gross * (1 - self.fee)

    def cost_to_buy(self, tokens: float) -> float:
        """Quote needed to extract exactly `tokens` from the pool. Diverges as tokens -> R."""
        if tokens <= 0:
            return 0.0
        if tokens >= self.R:
            return math.inf
        gross = self.k / (self.R - tokens) - self.Q
        return gross / (1 - self.fee)

    def after_buy(self, spend: float) -> "Pool":
        net = spend * (1 - self.fee)
        out = self.R - self.k / (self.Q + net)
        return Pool(self.R - out, self.Q + net, self.fee)

    def after_sell(self, tokens: float) -> "Pool":
        gross = self.Q - self.k / (self.R + tokens)
        return Pool(self.R + tokens, self.Q - gross, self.fee)

    # ── the two identities ────────────────────────────────────────────────────
    def cost_to_push(self, multiple: float) -> float:
        """Quote needed to move spot price by `multiple` (1.20 = +20%).

        Derivation: at the target price P' = m*P, R' = sqrt(k/P') so the quote added is
        sqrt(k*P') - Q = Q(sqrt(m) - 1). Fee applies to what we put in.
        """
        if multiple <= 1:
            return 0.0
        return self.Q * (math.sqrt(multiple) - 1) / (1 - self.fee)

    def tokens_from_push(self, multiple: float) -> float:
        """Tokens acquired by that push. A push is acquisition, not spend."""
        if multiple <= 1:
            return 0.0
        return self.R * (1 - 1 / math.sqrt(multiple))

    def extract_for_drawdown(self, drawdown: float) -> float:
        """Quote obtainable by selling until spot has fallen by `drawdown` (0.15 = -15%)."""
        if not 0 < drawdown < 1:
            return 0.0
        return self.Q * (1 - math.sqrt(1 - drawdown)) * (1 - self.fee)

    def tokens_for_drawdown(self, drawdown: float) -> float:
        if not 0 < drawdown < 1:
            return 0.0
        return self.R * (1 / math.sqrt(1 - drawdown) - 1)

    def price_after_buy(self, spend: float) -> float:
        return self.after_buy(spend).spot

    def avg_price(self, spend: float) -> float:
        t = self.buy(spend)
        return spend / t if t else math.inf


# ── fee calibration ───────────────────────────────────────────────────────────
@dataclass
class Calibration:
    fee: float
    spread: float            # max-min of the per-sample implied fee; big = not a constant fee
    samples: list[dict]
    ok: bool                 # did it behave like a constant multiplicative fee?

    @property
    def summary(self) -> str:
        return (f"fee {self.fee:.2%} (spread {self.spread*100:.2f}pp, n={len(self.samples)}) "
                f"{'constant' if self.ok else 'NOT CONSTANT — quotes must drive sizing'}")

    def accurate_to(self, tolerance: float = 0.005) -> float:
        """Largest fill size the calibrated model predicts within `tolerance`.

        THE MODEL DOES NOT PROMISE UNIFORM ACCURACY, and pretending otherwise is how a ladder
        gets sized on a number that was never that good. On the first token calibrated the
        implied fee drifts DOWN as size grows , most plausibly
        because the router begins splitting across the eleven dust venues once an order is large
        enough to be worth it. A single constant therefore fits small fills well and large ones
        loosely: error grew from a rounding error at small size to a couple of percent at the top.

        Callers must treat this as a hard boundary: below it, use the model (free, instant);
        above it, take a live quote. `advice.py` enforces exactly that.
        """
        good = [s["usd_in"] for s in self.samples if abs(s.get("pred_err", 0.0)) <= tolerance]
        return max(good) if good else 0.0

    @property
    def max_error(self) -> float:
        return max((abs(s.get("pred_err", 0.0)) for s in self.samples), default=0.0)


def calibrate(pool: Pool, samples: list[tuple[float, float]],
              tolerance: float = 0.012) -> Calibration:
    """Recover the per-trade fee from real quotes.

    `samples` are (quote_in_usd, tokens_out_valued_at_spot_usd) pairs from a quote ladder against
    a ZERO-FEE pool model. For each we solve

        total = 1 - out/in ;  impact = modelled loss from x*y=k alone
        fee   = 1 - (1 - total) / (1 - impact)

    A tight spread across a wide size range means one constant fee explains everything, and the
    curve model plus that constant is then trustworthy. A wide spread means something
    size-dependent is happening and the caller must price from live quotes instead.
    """
    base = Pool(pool.R, pool.Q, 0.0)
    fees: list[float] = []
    rows: list[dict] = []
    for usd_in, usd_out in samples:
        if usd_in <= 0 or usd_out <= 0:
            continue
        total = 1 - usd_out / usd_in
        toks = base.buy(usd_in)
        impact = 1 - (toks * base.spot) / usd_in
        if impact >= 1:
            continue
        fee = 1 - (1 - total) / (1 - impact)
        fees.append(fee)
        rows.append({"usd_in": usd_in, "usd_out": usd_out,
                     "total": total, "impact": impact, "fee": fee})
    if not fees:
        return Calibration(0.0, 0.0, [], ok=False)
    spread = max(fees) - min(fees)
    fitted = sum(fees) / len(fees)

    # Second pass: how well does that single fee actually PREDICT each sample? This is the
    # number callers need — an implied-fee spread of 0.3pp still leaves a 1.75% output error at
    # the top of the range, because impact and fee compound.
    fitted_pool = Pool(pool.R, pool.Q, fitted)
    for r in rows:
        predicted = fitted_pool.buy(r["usd_in"]) * base.spot
        r["pred_err"] = (predicted - r["usd_out"]) / r["usd_out"] if r["usd_out"] else 0.0

    return Calibration(fitted, spread, rows, ok=spread <= tolerance)


def k_stability(observations: list[tuple[float, float]]) -> tuple[bool, float]:
    """Does x*y=k hold across observations? Returns (stable, max_relative_drift).

    If k drifts, liquidity is being added or removed, or the position is concentrated -- either
    way the constant-product model no longer describes the venue and the caller should price from
    quotes rather than from reserves.
    """
    ks = [r * q for r, q in observations if r > 0 and q > 0]
    if len(ks) < 2:
        return True, 0.0
    lo, hi = min(ks), max(ks)
    drift = (hi - lo) / hi
    return drift < 0.01, drift
