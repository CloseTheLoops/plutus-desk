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
    def ceiling_share(self) -> float:
        """The most we could ever hold: everything we have plus the entire float."""
        return self.share(self.ours + self.float_)

    def reachable(self, target_share: float) -> bool:
        return target_share <= self.ceiling_share + 1e-12

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
             "note": "staking / vesting — not float, not ours"},
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

    balances = db.latest_balances(token_id)
    notes: list[str] = []

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
        notes.append(f"{len(missing)} of our wallets have no balance observation yet")

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
