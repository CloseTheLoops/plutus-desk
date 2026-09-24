"""Curve arithmetic, checked against REAL measured quotes — not against itself.

The numbers below are seven live `order quote` results from the first token onboarded, recorded
in .forge/research/2026-09-23_data_shapes.md. Checking the model against its own output would
prove nothing; checking it against what the venue actually paid is the only test that matters.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plutus.analyze.curve import Pool, calibrate, k_stability  # noqa: E402

# live pool state at the time the quotes were taken
R0, Q0 = 111_667_005.5598, 14_837.0
SPOT = Q0 / R0

# (usd_in, usd_value_out) straight from the vendor's own amount_in_usd / amount_out_usd
QUOTES = [
    (100.064189754, 94.487673880),
    (250.160474385, 233.89),
    (500.320948770, 460.21029928),
    (1000.641897540, 891.57),
    (2501.604743850, 2037.38),
    (5003.209487700, 3564.25),
    (10006.418975400, 5700.19),
]


def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def test_k_is_invariant_under_trading():
    p = Pool(R0, Q0, fee=0.0)
    k0 = p.k
    after = p.after_buy(5_000).after_sell(3_000_000).after_buy(120)
    assert approx(after.k, k0, 1e-12), "constant product must survive a round trip"


def test_buy_and_cost_to_buy_are_inverses():
    p = Pool(R0, Q0, fee=0.0483)
    for spend in (100, 1_000, 25_000):
        tokens = p.buy(spend)
        assert approx(p.cost_to_buy(tokens), spend, 1e-9)


def test_cost_to_push_matches_the_long_way_round():
    """Q(sqrt(m)-1) must equal solving the curve for the target price."""
    p = Pool(R0, Q0, fee=0.0)
    for m in (1.05, 1.2, 2.0, 5.0):
        target = p.spot * m
        R_target = math.sqrt(p.k / target)
        the_long_way = p.k / R_target - p.Q
        assert approx(p.cost_to_push(m), the_long_way, 1e-9)
        # and the push must actually land on the target price
        assert approx(p.after_buy(p.cost_to_push(m)).spot, target, 1e-9)


def test_tokens_from_push_is_what_the_spend_buys():
    p = Pool(R0, Q0, fee=0.0)
    for m in (1.1, 1.5, 3.0):
        assert approx(p.tokens_from_push(m), p.buy(p.cost_to_push(m)), 1e-9)


def test_extract_for_drawdown_lands_on_the_drawdown():
    p = Pool(R0, Q0, fee=0.0)
    for d in (0.05, 0.15, 0.30):
        toks = p.tokens_for_drawdown(d)
        assert approx(p.after_sell(toks).spot, p.spot * (1 - d), 1e-9)
        assert approx(p.sell(toks), p.extract_for_drawdown(d), 1e-9)


def test_extraction_is_linear_in_Q():
    """The claim the whole event playbook rests on: double the pool depth, double the exit."""
    a = Pool(R0, Q0, fee=0.0).extract_for_drawdown(0.15)
    b = Pool(R0, Q0 * 2, fee=0.0).extract_for_drawdown(0.15)
    assert approx(b, a * 2, 1e-9)


def test_calibration_recovers_the_real_fee():
    """THE acceptance check for stage 2: from a cold start, recover the venue's fee from quotes."""
    p = Pool(R0, Q0, fee=0.0)
    cal = calibrate(p, QUOTES)
    assert cal.ok, f"fee should look constant, got spread {cal.spread:.4f}"
    assert 0.040 <= cal.fee <= 0.055, f"expected ~4.8%, got {cal.fee:.4f}"
    assert cal.spread < 0.006, f"spread across a 100x size range too wide: {cal.spread:.4f}"


def test_calibrated_model_predicts_real_quotes_in_the_ladder_range():
    """Stage-2 acceptance, stated honestly: accurate where a ladder actually fills.

    A ladder's rungs are hundreds to low thousands of dollars. The model must be tight there.
    It is NOT uniformly tight — see test_model_states_its_own_accuracy_envelope — and the engine
    is required to take a live quote above the envelope rather than extrapolate.
    """
    cal = calibrate(Pool(R0, Q0, fee=0.0), QUOTES)
    p = Pool(R0, Q0, fee=cal.fee)
    for usd_in, usd_out in QUOTES:
        if usd_in > 2_600:            # above a plausible rung; quotes take over
            continue
        err = abs(p.buy(usd_in) * SPOT - usd_out) / usd_out
        assert err < 0.007, f"${usd_in:,.0f} predicted {err:.3%} off, over the 0.7% ladder bar"


def test_model_states_its_own_accuracy_envelope():
    """The model must not silently claim uniform accuracy it does not have."""
    cal = calibrate(Pool(R0, Q0, fee=0.0), QUOTES)
    assert cal.accurate_to(0.005) >= 1_000, (
        f"model should be within 0.5% up to at least $1k, got ${cal.accurate_to(0.005):,.0f}")
    assert cal.max_error < 0.025, f"full-range error {cal.max_error:.3%} is worse than expected"
    # error should GROW with size — that is the documented router-splitting signature, and if it
    # ever stops holding the explanation is wrong and the envelope needs re-deriving.
    errs = [s["pred_err"] for s in sorted(cal.samples, key=lambda s: s["usd_in"])]
    assert errs == sorted(errs), "prediction error should increase monotonically with fill size"


def test_uncalibrated_model_is_badly_wrong_at_small_size():
    """Guards the reason the fee exists at all: without it, small fills look ~8x too cheap."""
    naive = Pool(R0, Q0, fee=0.0)
    usd_in, usd_out = QUOTES[0]
    modelled_cost = 1 - (naive.buy(usd_in) * SPOT) / usd_in
    real_cost = 1 - usd_out / usd_in
    assert real_cost / modelled_cost > 5, "the fee-free model should badly understate small fills"


def test_k_stability_flags_liquidity_changes():
    assert k_stability([(R0, Q0), (R0 / 2, Q0 * 2)])[0] is True
    stable, drift = k_stability([(R0, Q0), (R0, Q0 * 1.5)])
    assert not stable and drift > 0.3


def test_pool_cannot_be_drained():
    p = Pool(R0, Q0, fee=0.0)
    assert p.cost_to_buy(R0) == math.inf
    assert p.cost_to_buy(R0 * 2) == math.inf


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
