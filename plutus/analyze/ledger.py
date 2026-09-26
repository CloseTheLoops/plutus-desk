"""The float ledger: who holds the supply, reconciled.

TWO RULES THIS MODULE EXISTS TO ENFORCE. Both were learned by getting them wrong first.

1. **FLOAT IS THE RESIDUAL, never something found.** It is computed as
   `effective - ours - pool - locked`, so the classes are forced to sum to supply. If an address
   is misclassified the error lands *visibly in float* instead of silently disappearing. Every
   attempt to compute float by adding up third-party holders instead produces a number that
   quietly disagrees with supply and nobody notices for weeks.

2. **BURNT SUPPLY LEAVES THE DENOMINATOR.** Burnt is not a class that holds — it is a class that
   REMOVES. `effective = nominal - burnt`, and every share is quoted against effective. On the
   first token onboarded a launchpad bonding curve had perma-burnt 8.163% of nominal at
   graduation; counting it as supply understated the operator's position by 4.4pp and made a 70%
   target look like it needed 86.9% of the float when it actually needed 57.1%. The difference
   between an impossible campaign and a routine one was entirely in the denominator.

`UNACCOUNTED` is reported as a live term, never absorbed. A ledger that always sums to exactly
100% is a ledger that is hiding something.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from plutus import db


@dataclass
class Ledger:
    nominal: float
    burnt: float
    ours: float
    pool: float
    locked: float
    unaccounted: float
    ours_wallets: int = 0
    ours_holding: int = 0
    pool_venues: int = 0
    census_holders: int = 0
    census_ts: int | None = None
    balance_ts: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def effective(self) -> float:
        """The only denominator that means anything: nobody can ever hold burnt supply."""
        return max(0.0, self.nominal - self.burnt)

    @property
    def float_(self) -> float:
        """THE RESIDUAL. Not a sum over holders — what is left after every other class."""
        return max(0.0, self.effective - self.ours - self.pool - self.locked - self.unaccounted)

    def share(self, amount: float) -> float:
        return amount / self.effective if self.effective else 0.0

    @property
    def ours_share(self) -> float:
        return self.share(self.ours)

    @property
    def float_share(self) -> float:
        return self.share(self.float_)

    def needed_for(self, target_share: float) -> float:
        """Tokens needed to reach `target_share` OF EFFECTIVE SUPPLY."""
        return max(0.0, target_share * self.effective - self.ours)

    @property
    def float_ceiling(self) -> float:
        """The share reachable from the FLOAT ALONE — everything we hold plus every third-party
        token, bought at something near market.

        THIS IS A COST BOUNDARY, NOT A HARD LIMIT, and an earlier version of this code wrongly
        treated it as the latter. Two corrections, both from the operator:

        - **The pool is buyable.** Only the LAST token costs infinity; a constant-product curve
          will sell you half its inventory for roughly its whole quote reserve. Expensive, and
          it moves the price enormously, but finite and sometimes worth it. See `with_pool`.
        - **Staking is not destroyed.** Stakers can unstake and sell. Those tokens are
          temporarily out of the market, not permanently unreachable, and they re-enter as
          float rather than being bought from the contract. See `if_unstaked`.

        Only BURNT supply is truly gone, which is why burnt is the one class removed from the
        denominator instead of merely set aside.
        """
        return self.share(self.ours + self.float_)

    def with_pool(self, pool_fraction: float) -> float:
        """Share reachable if we also buy `pool_fraction` of the pool's inventory."""
        return self.share(self.ours + self.float_ + self.pool * max(0.0, min(1.0, pool_fraction)))

    @property
    def if_unstaked(self) -> float:
        """Share reachable if everything currently staked unstaked and was sold to us."""
        return self.share(self.ours + self.float_ + self.locked)

    @property
    def absolute_ceiling(self) -> float:
        """Everything that is not burnt. The only genuinely permanent limit."""
        return self.share(self.effective - 0.0) if self.effective else 0.0

    def reachable(self, target_share: float) -> tuple[bool, str]:
        """Can we get there, and what stands in the way?

        Never a flat no unless the tokens genuinely do not exist. The engine's job is to price
        the difficulty, not to refuse a target because the cheap route runs out.
        """
        if target_share <= self.float_ceiling + 1e-12:
            return True, "float"
        if target_share <= self.with_pool(0.9) + 1e-12:
            return True, "pool"
        if target_share <= self.if_unstaked + 1e-12:
            return True, "unstake"
        if target_share <= 1.0 + 1e-12:
            return True, "extreme"
        return False, "impossible"

    def rows(self) -> list[dict]:
        out = [
            {"cls": "burnt", "tokens": self.burnt, "of_nominal": self.burnt / self.nominal
             if self.nominal else 0, "of_effective": None,
             "note": "gone forever — leaves the denominator"},
            {"cls": "ours", "tokens": self.ours, "of_nominal": self.ours / self.nominal
             if self.nominal else 0, "of_effective": self.ours_share,
             "note": f"{self.ours_holding} of {self.ours_wallets} wallets hold · direct query"},
            {"cls": "pool", "tokens": self.pool, "of_nominal": self.pool / self.nominal
             if self.nominal else 0, "of_effective": self.share(self.pool),
             "note": f"{self.pool_venues} venue(s)"},
            {"cls": "locked", "tokens": self.locked, "of_nominal": self.locked / self.nominal
             if self.nominal else 0, "of_effective": self.share(self.locked),
             "note": "staking / vesting — out of the market for now, NOT destroyed: "
                     "it can unstake and become float"},
            {"cls": "float", "tokens": self.float_, "of_nominal": self.float_ / self.nominal
             if self.nominal else 0, "of_effective": self.float_share,
             "note": f"the residual · {self.census_holders} holders seen in the census"},
        ]
        if self.unaccounted > 0:
            out.append({"cls": "unaccounted", "tokens": self.unaccounted,
                        "of_nominal": self.unaccounted / self.nominal if self.nominal else 0,
                        "of_effective": self.share(self.unaccounted),
                        "note": "classified but unexplained — never absorbed into float"})
        return out


def build(token_id: int) -> Ledger:
    """Assemble the ledger from stored observations. Pure read; no network."""
    tok = db.token_row(token_id)
    if tok is None:
        raise ValueError(f"unknown token_id {token_id}")

    nominal = float(tok["supply_nominal"] or 0)
    classes = db.classified(token_id)
    by_class: dict[str, list[str]] = {}
    for r in classes:
        by_class.setdefault(r["class"], []).append(r["address"])

    notes: list[str] = []

    # OUR POSITION IS ROLLED FORWARD FROM OUR OWN FILLS. Re-reading every wallet after every
    # buy wave costs one vendor call per wallet -- 500 wallets is 500 calls every few minutes,
    # far past the hourly budget -- while the trade feed already records, for free, every fill
    # those wallets made. So each of our wallets is its last real read plus the fills that read
    # cannot contain (db.fills_after has the exact rule). Real reads still happen: on first
    # sight, after a feed gap, and at the hourly reconciliation that catches what fills cannot
    # see (transfers, other venues, staking).
    rows = db.latest_balance_rows(token_id)
    balances = {a: (tok, h) for a, (tok, h, _obs) in rows.items()}
    ours_addrs = by_class.get("ours", [])
    state = {a: (rows[a][1], rows[a][2]) for a in ours_addrs if a in rows}
    rolled = db.fills_after(token_id, state)
    negative = []
    for a, (delta, _n) in rolled.items():
        tok, h = balances[a]
        if tok + delta < -1e-9:
            negative.append(a)
        balances[a] = (max(0.0, tok + delta), h)
    if rolled:
        n_fills = sum(n for _d, n in rolled.values())
        notes.append(f"{len(rolled)} of our wallets include {n_fills} fill(s) made since their "
                     f"last balance read, taken from the trade feed")
    if negative:
        notes.append(f"{len(negative)} of our wallets sold more than their last read held — "
                     f"tokens moved in from outside the tracked pool; shown as 0 until re-read")
    notes.extend(census_notes(token_id))
    from plutus.track.trackers import RECONCILE_S               # never imports this module
    overdue = [a for a in ours_addrs if a in rows and db.now() - rows[a][2] > RECONCILE_S + 600]
    if overdue:
        notes.append(f"{len(overdue)} of our wallets are past their {RECONCILE_S // 3600}h "
                     f"re-check — the API budget has been in use elsewhere. Their positions "
                     f"include every trade since, but not transfers or staking; a full pull "
                     f"re-reads them now.")
    moved = db.drift_since(token_id, db.now() - 86400)
    if moved:
        caught = max(r["ts"] for r in moved)
        behind = [a for a in ours_addrs if a in rows and rows[a][2] < caught]
        if behind:
            notes.append(f"tokens moved outside the trade feed (transfers, staking or another "
                         f"venue): {len(moved)} wallet(s) caught so far, net "
                         f"{sum(r['delta'] or 0 for r in moved):+,.0f}. {len(behind)} of our "
                         f"wallets were read before that and are being re-checked within "
                         f"~2h — until then our position may be off. A full pull corrects it now.")
    gap_ts = db.latest_tape_gap_ts(token_id)
    if gap_ts:
        behind = [a for a in ours_addrs if a in rows and rows[a][2] < gap_ts]
        if behind:
            notes.append(f"the trade feed missed some fills; {len(behind)} of our wallets were "
                         f"last read before that, so our position may be UNDERSTATED until they "
                         f"are re-read (the inventory sweep does this automatically)")

    def held(addresses: list[str]) -> tuple[float, int]:
        total, n = 0.0, 0
        for a in addresses:
            b = balances.get(a, (0.0, None))[0]
            total += b
            if b > 0:
                n += 1
        return total, n

    ours, ours_holding = held(by_class.get("ours", []))
    burnt, _ = held(by_class.get("burnt", []))
    pool, _ = held(by_class.get("pool", []))
    locked, _ = held(by_class.get("locked", []))
    unknown, unknown_n = held(by_class.get("unknown", []))

    if unknown_n:
        notes.append(f"{unknown_n} address(es) holding {unknown/nominal:.3%} of supply are "
                     f"UNCLASSIFIED — classify them before trusting any target")

    missing = [a for a in by_class.get("ours", []) if a not in balances]
    if missing:
        total_ours = len(by_class.get("ours", []))
        if missing and len(missing) == total_ours:
            notes.append(
                f"NO BALANCE WAS READ FOR ANY OF OUR {total_ours} WALLETS. Every figure below "
                f"that depends on what we hold is wrong, not zero. This is almost always the "
                f"gmgn profile failing to sign (an API key with no keypair.pem) — check the "
                f"inventory tick's detail line before reading anything on this page.")
        else:
            notes.append(f"{len(missing)} of our {total_ours} wallets have no balance "
                         f"observation yet — our position is UNDERSTATED by whatever they hold")

    census_ts = db.latest_census_ts(token_id)
    holders = 0
    if census_ts:
        known = set().union(*(set(v) for v in by_class.values())) if by_class else set()
        holders = sum(1 for r in db.census_rows(token_id, census_ts)
                      if (r["balance"] or 0) > 0 and r["address"] not in known
                      and (r["addr_type"] or 0) != 2)

    balance_ts = None
    row = db.connect().execute("SELECT MAX(observed_ts) t FROM balances WHERE token_id=?",
                               (token_id,)).fetchone()
    if row and row["t"]:
        balance_ts = row["t"]

    return Ledger(
        nominal=nominal, burnt=burnt, ours=ours, pool=pool, locked=locked,
        unaccounted=unknown,
        ours_wallets=len(by_class.get("ours", [])), ours_holding=ours_holding,
        pool_venues=len(by_class.get("pool", [])),
        census_holders=holders, census_ts=census_ts, balance_ts=balance_ts, notes=notes,
    )


# A census whose holder count falls this far below the best of the last day was cut short.
CENSUS_PARTIAL_RATIO = 0.6


def census_notes(token_id: int) -> list[str]:
    """How complete and how recent the holder census behind these numbers is.

    Always states age and coverage; warns (a note starting "CENSUS PARTIAL") when the latest
    census returned fewer slices than a full one, or found far fewer holders than the best census
    of the last day -- the 46-versus-132 case. Campaign advice carries that warning too.
    """
    from plutus.track.trackers import CENSUS_SLICES          # trackers never imports this module
    m = db.latest_census_meta(token_id)
    if m is None:
        return ["CENSUS PARTIAL: no holder census yet — holder-based figures are not available"]
    age_m = max(0, (db.now() - m["sweep_ts"]) // 60)
    found, holders = m["rows_found"] or 0, m["holder_count"] or 0
    cov = f"{found} of {holders} holders ({found / holders:.0%})" if holders else f"{found} holders"
    out = [f"census: {cov}, {age_m // 60}h {age_m % 60}m old"]
    best = max((r["rows_found"] or 0 for r in db.census_meta_since(token_id, db.now() - 86400)),
               default=found)
    if (m["slices"] or 0) < CENSUS_SLICES or (best and found < CENSUS_PARTIAL_RATIO * best):
        out.append(f"CENSUS PARTIAL: the latest census found {found} holders where the best of "
                   f"the last day found {best}, from {m['slices'] or 0} of {CENSUS_SLICES} "
                   f"slices — float composition and any advice drawn from holders use a partial "
                   f"view")
    return out
