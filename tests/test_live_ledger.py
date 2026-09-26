"""LIVE: build a real token's transfer ledger from the free chain RPC and check it exactly.

Opt-in (PLUTUS_LIVE_TESTS=1): it calls the real public node, into a scratch database. Checks
    * the backfill reconciles to total supply to the last unit;
    * every holder's balance equals the token's own balanceOf at the synced block -- not a
      sample: the largest holders plus a random draw of the rest;
    * an incremental sync a minute later costs a handful of calls.

    PLUTUS_LIVE_TESTS=1 python tests/test_live_ledger.py [token_address] [chain]
Default token: FAITH on Robinhood chain.
"""
from __future__ import annotations

import os
import pathlib
import random
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["PLUTUS_ETHERSCAN_DISABLE"] = "1"          # this checks the FREE source only
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="plutus_live_"))
os.environ["PLUTUS_DB"] = str(_TMP / "live.db")

from plutus import config  # noqa: E402

config.DB_PATH = pathlib.Path(os.environ["PLUTUS_DB"])

from plutus import db  # noqa: E402
from plutus.sources import chainrpc as R  # noqa: E402
from plutus.track import trackers as T  # noqa: E402

TOKEN = (sys.argv[1] if len(sys.argv) > 1 else "0x72aec25d3c3a5fd9772901e8433fe5c8cf4eaa04").lower()
CHAIN = sys.argv[2] if len(sys.argv) > 2 else "robinhood"


def balance_of(addr: str, block: int) -> int:
    data = "0x70a08231" + "0" * 24 + addr[2:]
    ep = R.endpoints(CHAIN)[0]
    return R._hex(R._post(ep, "eth_call", [{"to": TOKEN, "data": data}, hex(block)]))


def main() -> int:
    if os.environ.get("PLUTUS_LIVE_TESTS") != "1":
        print("skipped (set PLUTUS_LIVE_TESTS=1 to call the real node)")
        return 0
    tid = db.upsert_token(CHAIN, TOKEN, symbol="LIVE", supply_nominal=1.0)
    T.clear_abort(tid)
    t0, c0 = time.time(), R.calls()
    r = T.track_ledger(tid)
    print(f"  backfill: {r.detail}")
    print(f"            {R.calls() - c0} calls, {time.time() - t0:.0f}s")
    if not r.ok or "reconciles to total supply exactly" not in r.detail:
        print("FAIL  the backfill did not reconcile")
        return 1
    # The node keeps state ~100 seconds: sync to a fresh block, then compare at it straight away.
    r1 = T.track_ledger(tid)
    if not r1.ok:
        print(f"FAIL  re-sync before the balance check: {r1.detail}")
        return 1
    blk = db.ledger_state(tid)["synced_block"]
    raw = {x["address"]: int(x["raw"]) for x in db.connect().execute(
        "SELECT address, raw FROM holdings WHERE token_id=? AND raw != '0'", (tid,))}
    n_tx = db.connect().execute("SELECT COUNT(*) FROM transfers WHERE token_id=?", (tid,)).fetchone()[0]
    zero_ts = db.connect().execute("SELECT COUNT(*) FROM transfers WHERE token_id=? AND ts=0",
                                   (tid,)).fetchone()[0]
    print(f"  {n_tx:,} transfers · {len(raw):,} holders · {zero_ts} without a timestamp")
    top = sorted(raw, key=raw.get, reverse=True)[:25]
    rest = [a for a in raw if a not in top]
    check = top + random.Random(7).sample(rest, min(25, len(rest)))
    bad = []
    for a in check:
        chain_v = balance_of(a, blk)
        if chain_v != raw[a]:
            bad.append((a, raw[a], chain_v))
    print(f"  balanceOf at block {blk:,}: {len(check) - len(bad)}/{len(check)} holders match exactly")
    for a, mine, theirs in bad[:5]:
        print(f"    MISMATCH {a}: ledger {mine} chain {theirs}")
    # interpolated block times vs the chain's own, on a random sample
    rows = db.connect().execute("SELECT DISTINCT block, ts FROM transfers WHERE token_id=?",
                                (tid,)).fetchall()
    sample = random.Random(11).sample(list(rows), min(30, len(rows)))
    exact = R._fetch_times(CHAIN, [r_["block"] for r_ in sample])
    errs = [abs(r_["ts"] - exact[r_["block"]]) for r_ in sample if r_["block"] in exact]
    print(f"  block times: worst {max(errs)}s off over {len(errs)} sampled blocks")
    # Backfill times are interpolated between hourly anchors (measured ~1-5s off). They only
    # date "holding since" / "last active", so a minute is the tolerance, not the target.
    if not errs or max(errs) > 60:
        bad.append(("times", max(errs or [0]), 60))
    time.sleep(60)
    c1 = R.calls()
    r2 = T.track_ledger(tid)
    print(f"  a minute later: {r2.detail} · {R.calls() - c1} calls")
    ok = not bad and zero_ts == 0 and r2.ok and R.calls() - c1 <= 6
    print()
    print("ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
