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


def cmd_setpassword(a) -> None:
    """Set the admin password that guards every state-changing endpoint.

    Read from a prompt or PLUTUS_ADMIN_PASSWORD, never from a command-line argument -- an
    argument lands in shell history and in the process list where other users can see it.
    """
    import getpass
    import os

    from plutus.web import auth

    pw = os.environ.get("PLUTUS_ADMIN_PASSWORD") or ""
    if not pw:
        pw = getpass.getpass("new admin password: ")
        if pw != getpass.getpass("confirm: "):
            print("  they do not match")
            raise SystemExit(2)
    try:
        auth.set_password(pw)
    except ValueError as exc:
        print(f"  {exc}")
        raise SystemExit(2) from None
    print(f"  admin password set. Stored as a scrypt hash in {auth.PATH}")
    print("  That file is gitignored and must stay out of the repository.")


def cmd_serve(a) -> None:
    import os

    import uvicorn

    # The app has to know what it bound to. "A request from 127.0.0.1 is the operator" is true
    # only when nothing can sit in front of the server -- and behind a reverse proxy, every
    # request arrives from 127.0.0.1.
    os.environ["PLUTUS_BIND_HOST"] = a.host
    if a.host not in ("127.0.0.1", "::1", "localhost") and not os.environ.get("PLUTUS_KEY"):
        print()
        print(f"  WARNING: serving on {a.host} with no PLUTUS_KEY set.")
        print("  Everyone who can reach this port - including you - gets a read-only view.")
        print("  Set PLUTUS_KEY first, then open the page once with ?k=<your key>.")
        print()
    uvicorn.run("plutus.web.app:app", host=a.host, port=a.port, log_level="info")


def cmd_doctor(a) -> None:
    """Walk the balance chain in dependency order and name the first broken link.

    WHY THIS EXISTS. "Wallets show no balance" has at least six causes that look identical on the
    page: the wrong gmgn profile, an incomplete .env, a valid key with no entitlement, a token
    that was never onboarded, wallets that were never classified as ours, and a sweep that was
    never run. Guessing between them from a screenshot costs more than checking them in order.

    The last check is the one that matters: it calls the vendor directly for one of our wallets
    and compares the answer to what the database holds. That separates "the API returns zero"
    from "we never asked".
    """
    import os

    from plutus.sources import gmgn

    def line(ok, label, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  —  {detail}" if detail else ""))
        return ok

    _doctor_etherscan(a, line)

    print()
    print("gmgn credentials")
    home = gmgn.ANALYTICS_HOME
    env_path = os.path.join(home, ".config", "gmgn", ".env")
    if not os.path.isfile(env_path):
        line(True, "profile", f"no .env at {home} — inheriting the CLI's default profile")
    else:
        try:
            gmgn._env()
            body = open(env_path, encoding="utf-8").read()
            kid = ""
            for ln in body.splitlines():
                if ln.startswith("GMGN_API_KEY="):
                    kid = ln.split("=", 1)[1].strip()[:13] + "…"
            line(True, "profile", f"{home} · key {kid}")
        except gmgn.GmgnError as exc:
            line(False, "profile", str(exc)[:160])
            return

    b = gmgn.budget()
    if b.get("hour") is not None:
        line(b["ok"], "call budget",
             f"{b['hour']}/{b['hour_cap']} this hour · {b['day']}/{b['day_cap']} today"
             + ("" if b["ok"] else "  — SPENT; calls are being refused, not queued"))

    try:
        gmgn.call("gas-price", "--chain", a.chain, attempts=1)
        line(True, "api key accepted", "unsigned call succeeded (gas-price)")
    except Exception as exc:                                       # noqa: BLE001
        line(False, "api key accepted", str(exc)[:160])
        return
    try:
        bound = gmgn.bound_wallets()
        line(True, "private key signs", f"portfolio info returned {len(bound)} bound wallet(s)")
        if not bound:
            print("        note: no wallets are BOUND to this key. token-balance does not "
                  "require binding, so this is only a problem if you meant to use it as the "
                  "authoritative source for the OURS set.")
    except Exception as exc:                                       # noqa: BLE001
        line(False, "private key signs", str(exc)[:160])
        return

    if not a.token:
        print()
        print("(pass a token name for the per-token checks)")
        print()
        return

    print()
    print(f"token '{a.token}'")
    try:
        tid, cfg = _token_id(a.token)
    except SystemExit:
        return
    line(True, "onboarded", f"id={tid} chain={cfg.chain}")

    classes: dict[str, int] = {}
    for r in db.classified(tid):
        classes[r["class"]] = classes.get(r["class"], 0) + 1
    ours = classes.get("ours", 0)
    line(bool(classes), "addresses classified", str(classes) if classes else
         "NOTHING classified — paste your wallet list on /add, or run: "
         f"python -m plutus.cli worksheet {a.token}")
    if not ours:
        line(False, "wallets marked 'ours'",
             "zero. The ledger has nothing to sum, so it reports that we hold nothing. "
             "This is the most common cause of an empty desk.")
        return

    bal = db.latest_balances(tid)
    seen = [x for x in db.classified(tid, "ours") if x["address"] in bal]
    line(bool(bal), "balance observations",
         f"{len(bal)} rows · {len(seen)} of {ours} of our wallets have one"
         if bal else "NONE. Run a full pull: python -m plutus.cli tick "
                     f"{a.token} --full")

    print()
    print("live probe (vendor vs database, one wallet)")
    w = db.classified(tid, "ours")[0]["address"]
    stored = bal.get(w, (None, None))[0]
    try:
        live, height = gmgn.token_balance(cfg.chain, w, cfg.address)
    except Exception as exc:                                       # noqa: BLE001
        line(False, "vendor call", f"{w[:12]}… -> {str(exc)[:120]}")
        return
    line(True, "vendor call", f"{w[:12]}… -> {live:,.4f} tokens (height {height})")
    if live > 0 and not stored:
        line(False, "database agrees",
             "the vendor reports a balance the database does not have. The sweep never "
             f"stored it — run: python -m plutus.cli tick {a.token} --full  and read the "
             "tick's detail line for failed reads.")
    elif live == 0:
        line(False, "database agrees",
             "the vendor itself reports zero for this wallet. Check the token address and "
             "chain in your token config, and that this wallet is on that chain.")
    else:
        line(True, "database agrees", f"stored {stored:,.4f}")
    print()


def _doctor_etherscan(a, line) -> None:
    """The transfer ledger's source: key, chain on this plan, and -- per token -- exactness.

    Four calls at most. Never prints the key itself, only where it was found.
    """
    import os

    from plutus.sources import etherscan as es

    print()
    print("etherscan (transfer ledger)")
    key = es.api_key()
    where = next((n for n in ("PLUTUS_ETHERSCAN_KEY", "ETHERSCAN_API_KEY")
                  if (os.environ.get(n) or "").strip()), "data/etherscan.key")
    if not key:
        line(False, "api key", "none — balances fall back to per-wallet GMGN reads. Put the key "
             "on one line in data/etherscan.key (gitignored) or set PLUTUS_ETHERSCAN_KEY")
        return
    line(True, "api key", f"from {where} ({len(key)} chars)")
    cid = config.chain(a.chain).etherscan_chain
    if cid is None:
        line(False, "chain supported", f"{a.chain} has no Etherscan chain id in config")
        return
    try:
        head = es.latest_block(cid)
        line(True, "chain on this plan", f"{a.chain} (chainid {cid}) head block {head:,}")
    except es.PlanRequired as exc:
        line(False, "chain on this plan", str(exc)[:200])
        return
    except es.EtherscanError as exc:
        line(False, "chain on this plan", str(exc)[:160])
        return
    b = es.budget()
    if b.get("day") is not None:
        line(b["day"] < b["day_cap"], "daily budget", f"{b['day']:,}/{b['day_cap']:,} today "
             f"· paced at {es.RPS}/s")
    if not a.token:
        return
    try:
        tid, cfg = _token_id(a.token)
    except SystemExit:
        return
    st = db.ledger_state(tid)
    if not st:
        line(False, "ledger built", "not yet — the server builds it on its next pass, or run "
             f"a full pull for {a.token}")
        return
    supply = es.token_supply(cid, cfg.address)
    held = db.holdings_sum_raw(tid)
    line(held == supply, "ledger sums to supply",
         f"{len(db.holdings(tid)):,} holders · synced to block {st['synced_block']:,}"
         + ("" if held == supply else f" · off by {supply - held} raw units (rebuild due)"))
    line(db.ledger_healthy(tid), "exact mode",
         "on — balances are exact" if db.ledger_healthy(tid) else
         "off — not synced in the last 15 min or not yet verified; GMGN reads cover meanwhile")


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

    d = sub.add_parser("doctor", help="why are the balances zero?")
    d.add_argument("token", nargs="?"); d.add_argument("--chain", default="robinhood")
    d.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("setpassword", help="set the admin password for the web UI")
    sp.set_defaults(fn=cmd_setpassword)

    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8800); s.set_defaults(fn=cmd_serve)

    a = p.parse_args()
    try:
        a.fn(a)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
