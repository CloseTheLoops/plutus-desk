"""Tape-derived state: third-party flow, volatility, concentration, and the regime.

THE RULE THAT MAKES THIS MODULE WORTH HAVING: **every flow number is third-party only.**

Our own fills are separated at ingest, not at display. On the first token measured, 21 of our
wallets appeared in a 258-fill window carrying $1,399 of $6,401 volume — about a fifth of volume, all of it buying.
The raw tape read the market as strongly bid. With our own fills removed, roughly half the
apparent organic demand was our own money. Sizing a ladder against that number would mean
standing down from supply that was actually on offer, or chasing a rally we were producing
ourselves.

So `is_ours` is decided when a trade is written (see track/tape.py) and every aggregate here
filters on it. `test_flow.py` asserts no aggregate reads unsplit tape.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from plutus import db


@dataclass
class Flow:
    window_s: int
    third_buy: float = 0.0
    third_sell: float = 0.0
    third_buys: int = 0
    third_sells: int = 0
    our_buy: float = 0.0
    our_sell: float = 0.0
    our_fills: int = 0
    sigma: float = 0.0              # realised vol over the window, from fill prices
    sell_trend: float = 1.0         # recent sell flow vs the preceding stretch
    top_maker: str = ""
    top_maker_share: float = 0.0
    price_rank: float = 0.5         # where spot sits in its own 24h range, 0 = at the low
    fills: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def third_net(self) -> float:
        return self.third_buy - self.third_sell

    @property
    def our_share_of_volume(self) -> float:
        tot = self.third_buy + self.third_sell + self.our_buy + self.our_sell
        return (self.our_buy + self.our_sell) / tot if tot else 0.0

    @property
    def raw_net(self) -> float:
        """What a naive chart would show. Kept only to display the contamination."""
        return (self.third_buy + self.our_buy) - (self.third_sell + self.our_sell)


# Regimes. Each is a decision about WHERE THE BID SITS, never buy-or-wait: a resting bid costs
# nothing while it waits, so the only real lever is depth.
REGIMES = {
    "ACCELERATING": ("step the bid DOWN",
                     "Sells are arriving and the flow is still building. More supply is coming, "
                     "and cheaper — do not fill the first third of a dump."),
    "EXHAUSTING":   ("step the bid UP toward spot",
                     "Sells are arriving but decaying: the seller is finishing and price is about "
                     "to recover. This is the best fill window you get, and the one case where "
                     "crossing the spread is justified."),
    "STAND_DOWN":   ("hold the deep rungs, spend nothing",
                     "Net buying into the top of the range. Supply is not on offer, and anything "
                     "taken here is taken at the expensive end of the curve."),
    "QUIET":        ("rest the normal ladder",
                     "Both flows thin. The quotes cost nothing to leave up and a surprise seller "
                     "hits them."),
    "FARMED":       ("pull in — do NOT tighten",
                     "One address dominates the tape. Somebody is trading against you with a "
                     "reason, and tightening into that is how a maker is picked off."),
}

CONCENTRATION_ALARM = 0.25
ACCELERATING_AT = 1.3
EXHAUSTING_AT = 0.7


def measure(token_id: int, window_s: int = 900, our: set[str] | None = None) -> Flow:
    """Aggregate the tape over `window_s`, third-party and ours kept apart throughout."""
    conn = db.connect()
    now = db.now()
    lo = now - window_s
    rows = conn.execute(
        "SELECT ts, side, usd, price, maker, is_ours FROM trades WHERE token_id=? AND ts>=?",
        (token_id, lo)).fetchall()

    f = Flow(window_s=window_s, fills=len(rows))
    makers: dict[str, float] = {}
    prices: list[float] = []

    for r in rows:
        usd = float(r["usd"] or 0)
        is_ours = bool(r["is_ours"]) or (our is not None and (r["maker"] or "") in our)
        if is_ours:
            f.our_fills += 1
            if r["side"] == "buy":
                f.our_buy += usd
            else:
                f.our_sell += usd
            continue                       # our own flow never enters a third-party aggregate
        if r["side"] == "buy":
            f.third_buy += usd
            f.third_buys += 1
        else:
            f.third_sell += usd
            f.third_sells += 1
        makers[r["maker"] or "?"] = makers.get(r["maker"] or "?", 0.0) + 1
        if r["price"]:
            prices.append(float(r["price"]))

    if makers:
        f.top_maker, top_n = max(makers.items(), key=lambda kv: kv[1])
        f.top_maker_share = top_n / sum(makers.values())

    if len(prices) >= 3:
        rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
        if len(rets) >= 2:
            f.sigma = statistics.pstdev(rets)

    # sell trend: this window against the three preceding ones, third-party only
    prev = conn.execute(
        """SELECT COALESCE(SUM(usd),0) s FROM trades
           WHERE token_id=? AND ts>=? AND ts<? AND side='sell' AND is_ours=0""",
        (token_id, lo - 3 * window_s, lo)).fetchone()["s"] or 0.0
    prev_rate = prev / 3.0
    f.sell_trend = (f.third_sell / prev_rate) if prev_rate > 0 else (1.0 if f.third_sell == 0 else 2.0)

    # price rank in the 24h range
    day = conn.execute(
        """SELECT MIN(price) lo, MAX(price) hi FROM trades
           WHERE token_id=? AND ts>=? AND price>0""", (token_id, now - 86400)).fetchone()
    last = conn.execute(
        "SELECT price FROM trades WHERE token_id=? AND price>0 ORDER BY ts DESC LIMIT 1",
        (token_id,)).fetchone()
    if day and last and day["hi"] and day["lo"] is not None and day["hi"] > day["lo"]:
        f.price_rank = (float(last["price"]) - day["lo"]) / (day["hi"] - day["lo"])

    if f.our_share_of_volume > 0.15:
        f.notes.append(
            f"our own fills are {f.our_share_of_volume:.1%} of volume — a raw chart would show "
            f"net {f.raw_net:+,.0f} instead of {f.third_net:+,.0f}")
    if f.fills == 0:
        f.notes.append("no fills in this window — the tape has nothing to say yet")
    return f


def regime(f: Flow) -> tuple[str, str, str]:
    """Classify the tape. Returns (regime, action, why)."""
    if f.top_maker_share >= CONCENTRATION_ALARM and f.third_buys + f.third_sells >= 10:
        r = "FARMED"
    elif f.fills < 4:
        r = "QUIET"
    elif f.third_sell > 0 and f.sell_trend >= ACCELERATING_AT:
        r = "ACCELERATING"
    elif f.third_sell > 0 and f.sell_trend <= EXHAUSTING_AT and f.price_rank <= 0.5:
        r = "EXHAUSTING"
    elif f.third_net > 0 and f.price_rank >= 0.7:
        r = "STAND_DOWN"
    else:
        r = "QUIET"
    action, why = REGIMES[r]
    return r, action, why


def capture_rate(token_id: int, window_s: int = 3600) -> float | None:
    """Our fills as a fraction of total sell flow: did the ladder sit where supply arrived?

    Low capture with falling price  -> the bid is too deep, supply is being sold past us.
    100% capture with falling price -> the bid is too shallow and we are the only buyer.
    Returns None when no supply was offered — that is 'no answer', not zero.
    """
    lo = db.now() - window_s
    r = db.connect().execute(
        """SELECT COALESCE(SUM(CASE WHEN is_ours=0 AND side='sell' THEN usd END),0) offered,
                  COALESCE(SUM(CASE WHEN is_ours=1 AND side='buy'  THEN usd END),0) taken
           FROM trades WHERE token_id=? AND ts>=?""", (token_id, lo)).fetchone()
    offered = float(r["offered"] or 0)
    return (float(r["taken"] or 0) / offered) if offered > 0 else None
