"""Onboarding: paste a chain + contract address, get a classified token.

The operator supplies two things. Everything else is discovered and then PROPOSED for
confirmation — setup is a reconciliation worksheet, not a form, because the ledger's correctness
depends on classifying every address that holds meaningful supply and only the operator can
confirm which wallets are theirs.

Pool addresses are deliberately NOT an input. Venue discovery finds them; the config exists only
to correct a wrong guess.

CLASSES: ours | pool | burnt | locked | unknown.  FLOAT IS NEVER ASSIGNED — it is the residual
computed in analyze/ledger.py, which is the only thing that forces the ledger to reconcile.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from plutus import config, db
from plutus.analyze.curve import Pool, calibrate
from plutus.sources import gecko, gmgn

log = config.get_logger("onboard")

# Addresses that are burns on any EVM chain. A launchpad bonding curve is found separately.
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}
STABLE_SYMBOLS = {"USDG", "USDC", "USDT", "DAI", "USDE", "FDUSD", "TUSD", "BUSD"}

# Quote-ladder sizes for the fee calibration. Spans 100x so a constant multiplicative fee is
# distinguishable from anything size-dependent.
CALIBRATION_LADDER = (100, 250, 500, 1_000, 2_500, 5_000, 10_000)


@dataclass
class Discovery:
    token_id: int
    symbol: str = ""
    supply_nominal: float = 0.0
    holder_count: int = 0
    venues: list[dict] = field(default_factory=list)
    primary: str = ""
    quote_symbol: str = ""
    quote_token: str = ""
    quote_is_stable: bool = False
    launchpad: str = ""
    launch_status: str = ""
    vault: str = ""
    proposals: list[dict] = field(default_factory=list)
    calibration: dict | None = None
    notes: list[str] = field(default_factory=list)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def discover(cfg: config.TokenConfig, calibrate_fee: bool = True) -> Discovery:
    """Run the full discovery pass. Read-only; no private key is involved at any point."""
    ch = config.chain(cfg.chain)
    tid = db.upsert_token(cfg.chain, cfg.address)
    d = Discovery(token_id=tid)

    # ── token basics ──────────────────────────────────────────────────────────
    info = gmgn.token_info(cfg.chain, cfg.address)
    d.symbol = info.get("symbol") or ""
    d.supply_nominal = _f(info.get("circulating_supply")) or _f(info.get("total_supply"))
    d.holder_count = int(info.get("holder_count") or 0)
    launched = int(info.get("open_timestamp") or info.get("creation_timestamp") or 0)

    # ── every venue, not just the biggest ─────────────────────────────────────
    d.venues = gecko.venues(ch.gecko_network, cfg.address)
    if d.venues:
        d.primary = d.venues[0]["pool_address"]
        dust = [v for v in d.venues[1:] if v["reserve_usd"] < max(50.0, d.venues[0]["reserve_usd"] * 0.02)]
        if dust:
            d.notes.append(
                f"{len(d.venues)} venues found; {len(dust)} are dust "
                f"(${sum(v['reserve_usd'] for v in dust):,.0f} combined) — polled for migration only")
    now = db.now()
    for i, v in enumerate(d.venues):
        db.connect().execute(
            """INSERT INTO venues VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(token_id,pool_address) DO UPDATE SET
                 reserve_usd=excluded.reserve_usd, vol24=excluded.vol24,
                 is_primary=excluded.is_primary, last_seen=excluded.last_seen""",
            (tid, v["pool_address"], v["dex"], None, None,
             v["reserve_usd"], v["vol24"], None, 1 if i == 0 else 0, now))
    db.connect().commit()

    # ── the pool the vendor thinks matters, for reserves + quote asset ────────
    pool_info = gmgn.token_pool(cfg.chain, cfg.address)
    base_r, quote_r = _f(pool_info.get("base_reserve")), _f(pool_info.get("quote_reserve"))
    d.quote_symbol = pool_info.get("quote_symbol") or ""
    d.quote_token = pool_info.get("quote_address") or ""
    d.quote_is_stable = d.quote_symbol.upper() in STABLE_SYMBOLS
    if not d.quote_is_stable and d.quote_symbol:
        d.notes.append(f"quote asset {d.quote_symbol} is NOT a stablecoin — every bid carries "
                       f"unhedged {d.quote_symbol} exposure, which the engine must track")
    if base_r and quote_r:
        db.record_pool(tid, d.primary or pool_info.get("pool_address") or "?",
                       base_r, quote_r, _f(pool_info.get("liquidity")), "gmgn")

    # ── launchpad lineage: names the burn address and, often, the fee's owner ─
    vault = ""
    if calibrate_fee and d.quote_token and base_r:
        q = _probe_quote(cfg, d.quote_token, 100)
        launch = (q or {}).get("token_launch_info") or {}
        d.launchpad = launch.get("exchange") or ""
        d.launch_status = launch.get("status") or ""
        # The address that actually CUSTODIES the pool's tokens. On a v4 singleton this is the
        # PoolManager, and it appears only in the quote's route — `token pool` returns the pool
        # ID (a 32-byte hash), which holds nothing. Classifying the hash and not the manager is
        # how the POOL class silently reads zero.
        for step in (q or {}).get("steps") or []:
            if step.get("factoryAddress"):
                vault = step["factoryAddress"]
                break
    d.vault = vault

    db.upsert_token(cfg.chain, cfg.address, symbol=d.symbol, supply_nominal=d.supply_nominal,
                    launched_ts=launched or None, quote_token=d.quote_token or None,
                    quote_symbol=d.quote_symbol or None,
                    quote_is_stable=int(d.quote_is_stable), primary_venue=d.primary or None,
                    launchpad=d.launchpad or None, launch_status=d.launch_status or None)

    # ── propose classifications ───────────────────────────────────────────────
    d.proposals = _propose(cfg, d, pool_info, vault)

    # ── measure this token's own execution cost ───────────────────────────────
    if calibrate_fee and base_r and quote_r and d.quote_token:
        d.calibration = _calibrate(cfg, d, Pool(base_r, quote_r, 0.0))

    return d


def _probe_quote(cfg: config.TokenConfig, quote_token: str, usd: float) -> dict | None:
    """One read-only quote. Needs a `from` address only to shape the transaction; nothing signs."""
    frm = cfg.our_wallets[0] if cfg.our_wallets else "0x" + "0" * 40
    try:
        return gmgn.quote(cfg.chain, frm, quote_token, cfg.address, int(usd * 1e6))
    except gmgn.GmgnError as exc:
        log.warning("quote probe failed at $%s: %s", usd, exc)
        return None


def _calibrate(cfg: config.TokenConfig, d: Discovery, pool: Pool) -> dict | None:
    samples: list[tuple[float, float]] = []
    for usd in CALIBRATION_LADDER:
        q = _probe_quote(cfg, d.quote_token, usd)
        if not q:
            continue
        ui, uo = _f(q.get("amount_in_usd")), _f(q.get("amount_out_usd"))
        if ui > 0 and uo > 0:
            samples.append((ui, uo))
    if len(samples) < 3:
        d.notes.append("fee calibration skipped — too few quotes returned")
        return None
    cal = calibrate(pool, samples)
    db.record_calibration(d.token_id, cal.fee, cal.spread, True, pool.spot,
                          json.dumps(cal.samples))
    d.notes.append(cal.summary + f" · model accurate to ${cal.accurate_to(0.005):,.0f} within 0.5%")
    if not cal.ok:
        d.notes.append("FEE IS NOT CONSTANT across sizes — the engine must price from live "
                       "quotes rather than the curve model")
    return {"fee": cal.fee, "spread": cal.spread, "accurate_to": cal.accurate_to(0.005),
            "max_error": cal.max_error, "samples": cal.samples}


def _propose(cfg: config.TokenConfig, d: Discovery, pool_info: dict,
             vault: str = "") -> list[dict]:
    """Propose a class for every address we can reason about. Confirmed ones are not revisited."""
    tid, ch = d.token_id, cfg.chain
    out: list[dict] = []

    def prop(addr: str, cls: str, source: str, why: str, label: str = "",
             confirmed: bool = False) -> None:
        a = config.norm_addr(ch, addr)
        db.classify(tid, a, cls, source, label=label, evidence=why, confirmed=confirmed)
        out.append({"address": a, "class": cls, "source": source, "why": why,
                    "label": label, "confirmed": confirmed})

    # operator-declared, always wins
    for w in cfg.our_wallets:
        prop(w, "ours", "operator", "declared by the operator", confirmed=True)
    for addr, cls in cfg.excluded.items():
        prop(addr, cls, "operator", "declared by the operator", confirmed=True)
    for p in cfg.pools:
        prop(p, "pool", "operator", "declared by the operator", confirmed=True)

    # key-bound wallets are ours by construction
    try:
        for w in gmgn.bound_wallets():
            prop(w, "ours", "auto:portfolio_info", "bound to the API key")
    except gmgn.GmgnError as exc:
        log.warning("portfolio info unavailable: %s", exc)

    # Venue contracts — but ONLY where the identifier is a real address. A v4 pool ID is a hash
    # and holds nothing; those stay in the `venues` table, which is where they belong.
    for v in d.venues:
        pa = v["pool_address"] or ""
        if v["reserve_usd"] > 0 and config.is_address(ch, pa):
            prop(pa, "pool", "auto:gt_venue",
                 f"GeckoTerminal venue on {v['dex']} holding ${v['reserve_usd']:,.0f}",
                 label=v.get("name") or "")
    # The custodying contract. A venue's pool ID is NOT an address that holds tokens; on a v4
    # singleton every pool's balance sits at the PoolManager. Without this the POOL class is 0.
    for v in (vault, pool_info.get("base_vault_address"), pool_info.get("quote_vault_address")):
        if v and config.is_address(ch, str(v)):
            prop(v, "pool", "auto:vault", "vault / PoolManager custodying the pool's tokens")

    for b in BURN_ADDRESSES:
        prop(b, "burnt", "auto:burn_address", "standard burn address")

    return out


def worksheet(token_id: int, min_share: float = 0.002) -> list[dict]:
    """Every address holding at least `min_share`, with its current class.

    AN UNCLASSIFIED WALLET IS FLOAT, AND THAT IS THE CORRECT ANSWER — not an alarm. Float is the
    residual by design, so a third-party holder having no class is the normal case; the first
    version of this screen flagged every ordinary holder as UNKNOWN and told the operator to
    classify them before trusting any target, which is how a worksheet becomes something you
    learn to ignore.

    What genuinely deserves attention is narrower: an address that does not look like an ordinary
    wallet (a contract, by `addr_type`) or one large enough that misfiling it would move the
    ledger. Those come back with `needs_review` set; everything else is `float (assumed)`.
    """
    tok = db.token_row(token_id)
    nominal = float(tok["supply_nominal"] or 0) if tok else 0
    cls = db.class_map(token_id)
    # why each proposal was made — the worksheet is only useful if the operator can see the
    # reasoning, not just the verdict
    ev = {r["address"]: (r["evidence"] or "") for r in db.classified(token_id)}
    rows: dict[str, dict] = {}

    # With an exact transfer ledger, EVERY holder's balance is known -- use it. Otherwise the
    # per-wallet reads, which cover only the wallets that were read.
    if db.ledger_healthy(token_id):
        balances, seen = db.holdings(token_id), "ledger"
    else:
        balances, seen = db.latest_balances(token_id), "balance"
    for a, (bal, height) in balances.items():
        if bal > 0:
            rows[a] = {"address": a, "tokens": bal, "height": height,
                       "class": cls.get(a, "unknown"), "seen": seen,
                       "evidence": ev.get(a, "")}
    for r in db.census_rows(token_id):
        bal = float(r["balance"] or 0)
        if bal <= 0 or (nominal and bal / nominal < min_share):
            continue
        rows.setdefault(r["address"], {
            "address": r["address"], "tokens": bal, "height": None,
            "class": cls.get(r["address"], "unknown"), "seen": "census",
            "evidence": ev.get(r["address"]) or (
                f"seen in the census · {r['tags']}" if r["tags"] else "seen in the census"),
            "tags": r["tags"], "addr_type": r["addr_type"]})

    out = sorted(rows.values(), key=lambda x: -x["tokens"])
    for r in out:
        r["share"] = r["tokens"] / nominal if nominal else 0.0
        if r["class"] == "unknown":
            r["class"] = "float"
            r["assumed"] = True
            # a contract, or big enough that a misfile would visibly move the ledger
            r["needs_review"] = bool(r.get("addr_type")) or r["share"] >= 0.02
            if r["needs_review"] and not r.get("evidence"):
                r["evidence"] = ("looks like a contract, not a wallet" if r.get("addr_type")
                                 else f"holds {r['share']:.1%} of supply — confirm it is not yours")
        else:
            r["assumed"] = False
            r["needs_review"] = False
    return [r for r in out if r["share"] >= min_share or not r["assumed"]]
