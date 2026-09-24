"""Command line: onboard a token, run trackers, print the ledger, serve the page.

    python -m plutus.cli onboard mytoken          # discover + classify + calibrate the fee
    python -m plutus.cli tick mytoken --full      # run the trackers once
    python -m plutus.cli ledger mytoken           # the reconciled ledger
    python -m plutus.cli worksheet mytoken        # the classification worksheet
    python -m plutus.cli serve                  # the analysis page
"""
from __future__ import annotations

import argparse
import sys

from plutus import config, db, onboard
from plutus.analyze import composition as C
from plutus.analyze import flow as F
from plutus.analyze import ledger as L
from plutus.analyze.curve import Pool
from plutus.track import trackers as T


def _token_id(name: str) -> tuple[int, config.TokenConfig]:
    cfg = config.load_token(name)
    row = db.find_token(cfg.chain, cfg.address)
    if row is None:
        print(f"'{name}' is not onboarded yet — run: python -m plutus.cli onboard {name}")
        raise SystemExit(2)
    return row["id"], cfg


def _pool(tid: int) -> Pool | None:
    r = db.latest_pool(tid)
    if not r or not r["base_reserve"]:
        return None
    cal = db.latest_calibration(tid)
    return Pool(r["base_reserve"], r["quote_reserve"], cal["fee_pct"] if cal else 0.0)


def cmd_onboard(a) -> None:
    cfg = config.load_token(a.token)
    print(f"onboarding {cfg.label or cfg.address} on {cfg.chain}…")
    d = onboard.discover(cfg, calibrate_fee=not a.no_calibrate)
    print(f"\n  {d.symbol}  supply {d.supply_nominal:,.0f}  holders {d.holder_count}")
    print(f"  launchpad {d.launchpad or '?'} {d.launch_status or ''}")
    print(f"  quote {d.quote_symbol} (stable={d.quote_is_stable})")
    print(f"  venues {len(d.venues)}  primary {d.primary[:22]}…")
    if d.vault:
        print(f"  vault  {d.vault}   <- the address that actually custodies pool tokens")
    counts: dict[str, int] = {}
    for p in d.proposals:
        counts[p["class"]] = counts.get(p["class"], 0) + 1
    print(f"  classified {len(d.proposals)}: {counts}")
    if d.calibration:
        c = d.calibration
        print(f"  FEE {c['fee']:.3%} (spread {c['spread']*100:.2f}pp) · model accurate to "
              f"${c['accurate_to']:,.0f} within 0.5% · worst {c['max_error']:.2%}")
    for n in d.notes:
        print(f"  - {n}")


def cmd_tick(a) -> None:
    tid, _ = _token_id(a.token)
    for r in T.tick(tid, full_inventory=a.full, with_census=a.census):
        flag = "ok  " if r.ok else "FAIL"
        print(f"  {r.name:<10} {flag} {r.calls:>3} calls {r.seconds:>6.1f}s  {r.detail}")


def cmd_ledger(a) -> None:
    tid, _ = _token_id(a.token)
    led = L.build(tid)
    pool = _pool(tid)
    print(f"nominal {led.nominal:,.0f} · burnt {led.burnt:,.0f} · EFFECTIVE {led.effective:,.0f}")
    if pool:
        print(f"spot {pool.spot:.6e} · FDV ${pool.fdv(led.nominal):,.0f} · "
              f"Q ${pool.Q:,.0f} · fee {pool.fee:.2%}\n")
    print(f"{'class':<13}{'tokens':>16}{'% nominal':>11}{'% effective':>13}  note")
    for r in led.rows():
        eff = f"{r['of_effective']:.3%}" if r["of_effective"] is not None else "-"
        print(f"{r['cls']:<13}{r['tokens']:>16,.0f}{r['of_nominal']:>10.3%}{eff:>13}  {r['note']}")
    total = led.burnt + led.ours + led.pool + led.locked + led.float_ + led.unaccounted
    print(f"\nreconciles to {total:,.0f} vs nominal {led.nominal:,.0f} "
          f"(diff {total - led.nominal:+,.0f})")
    print(f"ours {led.ours_share:.3%} of effective · float {led.float_share:.3%} · "
          f"float ceiling {led.float_ceiling:.2%} "
          f"(+pool@50% -> {led.with_pool(0.5):.2%}, if staking unwinds -> {led.if_unstaked:.2%})")
    for n in led.notes:
        print(f"  ! {n}")

    fl = F.measure(tid, 900)
    reg, act, why = F.regime(fl)
    print(f"\nflow (15m, third-party only): buy ${fl.third_buy:,.0f} sell ${fl.third_sell:,.0f} "
          f"net ${fl.third_net:+,.0f} · ours excluded ${fl.our_buy + fl.our_sell:,.0f} "
          f"({fl.our_share_of_volume:.1%} of volume)")
    print(f"REGIME {reg} — {act}")
    if pool:
        comp = C.build(tid, pool.spot, float_true=led.float_)
        if comp.segments:
            print(f"\nfloat: {comp.holders} holders, {comp.exited} exited")
            for s in comp.segments[:4]:
                print(f"  {s.name:<20} {s.wallets:>4} wallets  {s.share_of_float:>6.1%} of float")
            for n in comp.notes:
                print(f"  ! {n}")


def cmd_worksheet(a) -> None:
    tid, _ = _token_id(a.token)
    rows = onboard.worksheet(tid, min_share=a.min_share)
    print(f"{'address':44}{'share':>9}  class      source")
    cls = {r["address"]: r for r in db.classified(tid)}
    for r in rows:
        src = cls.get(r["address"], {})
        flag = "!" if r["class"] == "unknown" else " "
        print(f"{flag}{r['address']:43}{r['share']:>8.3%}  {r['class']:<10} "
              f"{src['source'] if src else ''}")
    unknown = [r for r in rows if r["class"] == "unknown"]
    if unknown:
        print(f"\n{len(unknown)} UNCLASSIFIED holding "
              f"{sum(r['share'] for r in unknown):.3%} of supply — classify before trusting "
              f"any target. Add them to the token's [excluded] table or the wallets file.")


def cmd_serve(a) -> None:
    import uvicorn
    uvicorn.run("plutus.web.app:app", host=a.host, port=a.port, log_level="info")


def main() -> None:
    p = argparse.ArgumentParser(prog="plutus", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("onboard"); o.add_argument("token")
    o.add_argument("--no-calibrate", action="store_true"); o.set_defaults(fn=cmd_onboard)

    t = sub.add_parser("tick"); t.add_argument("token")
    t.add_argument("--full", action="store_true", help="full inventory pass, every address")
    t.add_argument("--census", action="store_true", help="also run the 35-call holder sweep")
    t.set_defaults(fn=cmd_tick)

    l = sub.add_parser("ledger"); l.add_argument("token"); l.set_defaults(fn=cmd_ledger)

    w = sub.add_parser("worksheet"); w.add_argument("token")
    w.add_argument("--min-share", type=float, default=0.002); w.set_defaults(fn=cmd_worksheet)

    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8800); s.set_defaults(fn=cmd_serve)

    a = p.parse_args()
    try:
        a.fn(a)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
