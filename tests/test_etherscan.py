"""The Etherscan client and the transfer ledger built on it. Never touches the real API.

What must hold, because every balance on the desk rests on it:
  * every response shape Etherscan actually returns is handled -- "no records" is an empty
    answer, not a failure; rate limits retry; a bad key or a chain that needs a paid plan says
    so plainly (Robinhood chain needs the Lite plan from 2026-10-16);
  * paging returns every transfer exactly once, even when one block holds more than a page;
  * transfers applied twice are counted once, and holdings are exact integers that sum to
    supply to the last unit;
  * a ledger that does not reconcile is rebuilt, never trusted.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="plutus_es_"))
os.environ["PLUTUS_DB"] = str(_TMP / "es.db")
os.environ["PLUTUS_ETHERSCAN_RPS"] = "1000"          # no real pacing wait in unit tests
os.environ["PLUTUS_ETHERSCAN_DISABLE"] = "1"         # the key comes from the patch below only

from plutus import config  # noqa: E402

config.DB_PATH = pathlib.Path(os.environ["PLUTUS_DB"])
from plutus import db  # noqa: E402
from plutus.sources import etherscan as E  # noqa: E402
from plutus.track import trackers as T  # noqa: E402

E.api_key = lambda: "test-key"
_ORIGINAL = {n: getattr(E, n) for n in ("decimals", "latest_block", "token_supply", "transfer_logs")}


def _restore():
    for n, f in _ORIGINAL.items():
        setattr(E, n, f)
TOKEN = "0x" + "7" * 40
ZERO = "0x" + "0" * 40
TOPIC = E.TRANSFER_TOPIC


class Resp:
    def __init__(self, body, status=200):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _script(*responses):
    """requests.get returning these, in order."""
    _restore()
    seq = list(responses)
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(params)
        return seq.pop(0)
    E.requests.get = get
    return calls


def _log(block, idx, frm, to, raw):
    return {"blockNumber": hex(block), "logIndex": hex(idx), "transactionHash": f"0x{block:032x}{idx:032x}",
            "topics": [TOPIC, "0x" + "0" * 24 + frm[2:], "0x" + "0" * 24 + to[2:]],
            "data": hex(raw), "timeStamp": hex(1_800_000_000 + block)}


# ── responses ────────────────────────────────────────────────────────────────────────
def test_status_one_returns_the_result():
    _script(Resp({"status": "1", "message": "OK", "result": "12345"}))
    assert E.token_supply(4663, TOKEN) == 12345


def test_no_records_is_an_empty_answer_not_an_error():
    _script(Resp({"status": "0", "message": "No records found", "result": []}))
    assert E.transfer_logs(4663, TOKEN, 0, 10) == []


def test_a_rate_limit_is_retried():
    _script(Resp({"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}),
            Resp({"status": "1", "message": "OK", "result": "7"}))
    assert E.token_supply(4663, TOKEN) == 7


def test_a_server_error_is_retried():
    _script(Resp({}, status=502), Resp({"status": "1", "message": "OK", "result": "9"}))
    assert E.token_supply(4663, TOKEN) == 9


def test_a_bad_key_says_so():
    _script(Resp({"status": "0", "message": "NOTOK", "result": "Invalid API Key"}))
    try:
        E.token_supply(4663, TOKEN)
        raise AssertionError("an invalid key was not reported")
    except E.NoKey:
        pass


def test_a_chain_that_needs_a_paid_plan_says_so():
    _script(Resp({"status": "0", "message": "NOTOK",
                  "result": "Free API access is not supported for this chain. Please upgrade."}))
    try:
        E.token_supply(4663, TOKEN)
        raise AssertionError("a plan restriction was not reported")
    except E.PlanRequired as exc:
        assert "Lite" in str(exc), "the message does not say what plan is needed"


def test_proxy_calls_unwrap_the_json_rpc_envelope():
    _script(Resp({"jsonrpc": "2.0", "id": 1, "result": "0x12"}))
    assert E.decimals(4663, TOKEN) == 18
    _script(Resp({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "bad"}}))
    try:
        E.latest_block(4663)
        raise AssertionError("an RPC error was not raised")
    except E.EtherscanError:
        pass


# ── paging ───────────────────────────────────────────────────────────────────────────
def _paging_server(logs, cap):
    _restore()
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(params)
        lo, hi = int(params["fromBlock"]), int(params["toBlock"])
        sel = sorted((x for x in logs if lo <= int(x["blockNumber"], 16) <= hi),
                     key=lambda x: (int(x["blockNumber"], 16), int(x["logIndex"], 16)))
        page, off = int(params["page"]), min(int(params["offset"]), cap)
        chunk = sel[(page - 1) * off: page * off]
        if not chunk:
            return Resp({"status": "0", "message": "No records found", "result": []})
        return Resp({"status": "1", "message": "OK", "result": chunk})
    E.requests.get = get
    return calls


def test_paging_returns_every_transfer_exactly_once():
    logs = [_log(100 + i // 3, i % 3, "0x" + "a" * 40, "0x" + "b" * 40, i + 1) for i in range(2500)]
    _paging_server(logs, cap=1000)
    got = E.transfer_logs(4663, TOKEN, 0, 10_000)
    assert len(got) == 2500, f"expected 2500 transfers, got {len(got)}"
    assert len({(g["tx_hash"], g["log_index"]) for g in got}) == 2500, "duplicates returned"
    assert sum(g["raw"] for g in got) == sum(range(1, 2501)), "values were lost or doubled"


def test_paging_survives_one_block_holding_more_than_a_page():
    logs = [_log(500, i, "0x" + "a" * 40, "0x" + "b" * 40, 1) for i in range(1500)]
    logs += [_log(501, 0, "0x" + "a" * 40, "0x" + "b" * 40, 1)]
    _paging_server(logs, cap=1000)
    got = E.transfer_logs(4663, TOKEN, 0, 10_000)
    assert len(got) == 1501, f"a crowded block lost transfers: {len(got)} of 1501"


# ── budget ───────────────────────────────────────────────────────────────────────────
def test_the_daily_cap_refuses_rather_than_spends():
    prev = E.DAILY_CAP
    E.DAILY_CAP = E.budget()["day"] + 2
    try:
        _script(Resp({"status": "1", "result": "1"}), Resp({"status": "1", "result": "1"}),
                Resp({"status": "1", "result": "1"}))
        E.token_supply(4663, TOKEN)
        E.token_supply(4663, TOKEN)
        try:
            E.token_supply(4663, TOKEN)
            raise AssertionError("a call went past the daily cap")
        except E.OverBudget:
            pass
    finally:
        E.DAILY_CAP = prev


# ── the ledger ───────────────────────────────────────────────────────────────────────
def _parsed(block, idx, frm, to, raw):
    return {"block": block, "log_index": idx, "tx_hash": f"0x{block:x}{idx:x}", "from": frm,
            "to": to, "raw": raw, "ts": block}


def test_holdings_are_exact_and_a_repeat_is_not_double_counted():
    tid = db.upsert_token("robinhood", "0x" + "1" * 40, symbol="L1", supply_nominal=1.0)
    a, b = "0x" + "a" * 40, "0x" + "b" * 40
    big = 10 ** 27 + 7                                      # beyond float precision on purpose
    logs = [_parsed(1, 0, ZERO, a, big), _parsed(2, 0, a, b, 3), _parsed(3, 0, b, ZERO, 1)]
    assert db.apply_transfers(tid, logs, 18, 3) == 3
    assert db.apply_transfers(tid, logs, 18, 3) == 0, "a re-fetched transfer was applied again"
    assert db.holdings_sum_raw(tid) == big - 1, "holdings are not exact (mint minus burn)"
    raw = {r["address"]: int(r["raw"]) for r in db.connect().execute(
        "SELECT address, raw FROM holdings WHERE token_id=?", (tid,))}
    assert raw[a] == big - 3 and raw[b] == 2


def _fake_chain(tid, logs, supply, head):
    E.decimals = lambda cid, tok: 18
    E.latest_block = lambda cid: head[0]
    E.token_supply = lambda cid, tok: supply[0]
    E.transfer_logs = lambda cid, tok, lo, hi: [x for x in logs if lo <= x["block"] <= hi]


def test_track_ledger_backfills_then_verifies_against_supply():
    tok = "0x" + "2" * 40
    tid = db.upsert_token("robinhood", tok, symbol="L2", supply_nominal=1.0)
    a = "0x" + "c" * 40
    logs = [_parsed(10, 0, ZERO, a, 1000), _parsed(20, 0, a, "0x" + "d" * 40, 400)]
    _fake_chain(tid, logs, [1000], [25])
    T.clear_abort(tid)
    r = T.track_ledger(tid)
    assert r.ok and "reconciles to total supply exactly" in r.detail, r.detail
    assert db.ledger_healthy(tid)


def test_a_ledger_off_supply_is_rebuilt_not_trusted():
    tok = "0x" + "3" * 40
    tid = db.upsert_token("robinhood", tok, symbol="L3", supply_nominal=1.0)
    logs = [_parsed(10, 0, ZERO, "0x" + "e" * 40, 1000)]
    _fake_chain(tid, logs, [999], [25])                  # supply disagrees with the transfers
    T.clear_abort(tid)
    r = T.track_ledger(tid)
    assert not r.ok and r.status == "failed"
    assert db.ledger_state(tid) is None, "a ledger that did not reconcile was kept"
    assert not db.ledger_healthy(tid)


def test_a_head_that_moves_between_reads_is_caught_up_not_rebuilt():
    tok = "0x" + "4" * 40
    tid = db.upsert_token("robinhood", tok, symbol="L4", supply_nominal=1.0)
    a = "0x" + "f" * 40
    logs = [_parsed(10, 0, ZERO, a, 1000), _parsed(30, 0, ZERO, a, 50)]   # a mint lands at 30
    head, supply = [25], [1050]
    _fake_chain(tid, logs, supply, head)

    real_supply = E.token_supply
    def supply_then_move(cid, t):                         # the chain moves on after the first read
        head[0] = 35
        return real_supply(cid, t)
    E.token_supply = supply_then_move
    T.clear_abort(tid)
    r = T.track_ledger(tid)
    assert r.ok, f"a moving head was treated as a broken ledger: {r.detail}"
    assert db.holdings_sum_raw(tid) == 1050


def test_the_ledger_drives_the_position_exactly_when_healthy():
    from plutus.analyze import ledger as L
    tok = "0x" + "5" * 40
    tid = db.upsert_token("robinhood", tok, symbol="L5", supply_nominal=2000.0)
    ours, pool = "0x" + "9" * 40, "0x" + "8" * 40
    db.classify(tid, ours, "ours", source="operator")
    db.classify(tid, pool, "pool", source="operator")
    logs = [_parsed(10, 0, ZERO, pool, 1500 * 10 ** 18), _parsed(10, 1, ZERO, ours, 500 * 10 ** 18),
            _parsed(20, 0, ours, "0x" + "7" * 40, 120 * 10 ** 18)]        # a transfer: no trade
    _fake_chain(tid, logs, [2000 * 10 ** 18], [25])
    T.clear_abort(tid)
    assert T.track_ledger(tid).ok
    led = L.build(tid)
    assert abs(led.ours - 380.0) < 1e-9, f"ours should be exactly 380 after the transfer, got {led.ours}"
    assert any("exact" in n for n in led.notes), "the page does not say the balances are exact"
    assert not any("past their" in n or "outside the trade feed" in n for n in led.notes), \
        "per-wallet-read warnings shown although the ledger makes them irrelevant"



def test_holders_come_from_the_ledger_with_never_bought_known_exactly():
    from plutus.analyze import holders as H
    tok = "0x" + "6" * 40
    tid = db.upsert_token("robinhood", tok, symbol="L6", supply_nominal=1000.0)
    pool = "0x" + "8" * 40
    buyer, gifted = "0x" + "b1" * 20, "0x" + "c1" * 20
    db.classify(tid, pool, "pool", source="operator")
    logs = [_parsed(10, 0, ZERO, pool, 900 * 10 ** 18), _parsed(10, 1, ZERO, "0x" + "d1" * 20, 100 * 10 ** 18),
            _parsed(20, 0, pool, buyer, 50 * 10 ** 18),                    # bought from the pool
            _parsed(21, 0, "0x" + "d1" * 20, gifted, 30 * 10 ** 18)]        # arrived by transfer
    _fake_chain(tid, logs, [1000 * 10 ** 18], [25])
    T.clear_abort(tid)
    assert T.track_ledger(tid).ok
    rows, complete = db.holder_rows(tid)
    by = {r["address"]: r for r in rows}
    assert complete, "a healthy ledger did not mark the holder list complete"
    assert gifted in by and by[gifted]["avg_cost"] == 0.0, "a transfer-only holder is not 'never bought'"
    assert by[buyer]["avg_cost"] is None, "a pool buyer with no known cost was filed as never bought"
    v = H.build(tid, 0.01, 150.0, 1000.0)
    segs = {h.address: h.segment for h in v.holders}
    assert segs[gifted] == "never bought" and segs[buyer] == "cost unknown", segs
    assert abs(v.coverage - 1.0) < 1e-9, f"coverage with a ledger should be complete, got {v.coverage}"

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
