"""Flow metrics must be right, or say they don't know.

Each test here pins a bug that shipped and produced a plausible-looking wrong number — the worst
kind, because nothing errors and the figure is simply believed.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plutus import config  # noqa: E402

config.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"   # never touch the real store

from plutus import db  # noqa: E402
from plutus.analyze import flow as F  # noqa: E402

TOKEN, SPOT = 1, 1.0e-4


def _seed(trades):
    """trades: (seconds_ago, side, usd, tokens)"""
    db.upsert_token("robinhood", "0x" + "b" * 40, supply_nominal=1e9)
    now = db.now()
    rows = [(TOKEN, f"0x{i:064x}", 0, "p", now - ago, side, usd, tok,
             (usd / tok) if tok else 0, f"0xmaker{i%7:034x}", 0, "test")
            for i, (ago, side, usd, tok) in enumerate(trades)]
    db.record_trades(rows)


def setup_module(_=None):
    db.connect().execute("DELETE FROM trades")
    db.connect().commit()


def test_price_is_the_token_price_on_both_sides():
    """A sell sends the token and receives the stablecoin. Taking the vendor's price-of-what-
    -you-received put ~1.00 in the same column as ~0.0001, and every range statistic over that
    column became noise: price rank read 0.99 while spot sat at the 24h LOW."""
    setup_module()
    _seed([(3600, "buy", 100.0, 1_000_000), (1800, "sell", 50.0, 500_000)])
    rows = db.connect().execute(
        "SELECT side, price FROM trades WHERE token_id=?", (TOKEN,)).fetchall()
    for r in rows:
        assert 1e-6 < r["price"] < 1e-2, (
            f"{r['side']} stored price {r['price']} — that is the quote asset, not the token")


def test_sell_trend_is_unknown_without_a_baseline():
    """With less tape than the baseline window needs, the divisor is mostly silence and the
    ratio is an artifact. It returned 18.18 on ~6h of a 72h baseline."""
    setup_module()
    _seed([(600, "sell", 1000.0, 10_000_000)])        # one recent sell, no history behind it
    f = F.measure(TOKEN, 86400)
    assert f.sell_trend is None, f"expected unknown, got {f.sell_trend}"
    assert any("baseline" in n for n in f.notes), "it should say why"


def test_sell_trend_computes_once_the_baseline_is_covered():
    setup_module()
    _seed([(600, "sell", 300.0, 3_000_000)]
          + [(3600 * h, "sell", 100.0, 1_000_000) for h in range(2, 14)])
    f = F.measure(TOKEN, 3600)
    assert f.sell_trend is not None, "baseline is covered, so it should produce a number"
    assert f.sell_trend > 1, "a burst above the baseline should read above 1"


def test_our_own_fills_never_enter_a_third_party_aggregate():
    setup_module()
    _seed([(600, "buy", 500.0, 5_000_000)])
    db.connect().execute("UPDATE trades SET is_ours=1 WHERE token_id=?", (TOKEN,))
    db.connect().commit()
    f = F.measure(TOKEN, 3600)
    assert f.third_buy == 0 and f.third_sell == 0, "ours leaked into third-party flow"
    assert f.our_buy == 500.0
    assert f.raw_net != f.third_net, "the contaminated figure should differ from the clean one"


def test_price_rank_does_not_pretend_with_too_few_prices():
    setup_module()
    _seed([(600, "sell", 50.0, 500_000), (1200, "sell", 50.0, 500_000)])
    f = F.measure(TOKEN, 86400)
    assert f.price_rank == 0.5, "two prices cannot locate spot in a range"
    assert any("price rank" in n for n in f.notes)


def test_net_selling_at_the_low_is_not_reported_as_quiet():
    """Losing one input should not discard the part we do know."""
    setup_module()
    # i=0 is the most recent; give it the MOST tokens per dollar so it is the LOWEST price,
    # i.e. spot has been falling and now sits at the bottom of the 24h range.
    _seed([(600 + i * 60, "sell", 100.0, 1_700_000 - i * 100_000) for i in range(8)])
    f = F.measure(TOKEN, 86400)
    r, action, _ = F.regime(f)
    assert r != "QUIET", f"net selling at the low read as {r}"
    assert r in ("SUPPLY_OFFERED", "ACCELERATING", "EXHAUSTING")


def test_capture_rate_is_none_when_nothing_was_offered():
    setup_module()
    _seed([(600, "buy", 100.0, 1_000_000)])
    assert F.capture_rate(TOKEN, 3600) is None, "no supply offered is 'no answer', not zero"


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
