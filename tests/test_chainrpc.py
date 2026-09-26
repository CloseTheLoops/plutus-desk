"""The free chain-RPC source for the transfer ledger. Never touches a real node.

A fake node reproduces what the public Robinhood RPC was measured to do (2026-09-26):
  * a cap on logs per query, and a timeout on wide busy ranges;
  * an EMPTY answer -- not an error -- for blocks past its head;
  * state kept for a short window only;
  * batches over a size refused with 429; blockTimestamp in logs always 0x0.
Asserted: the ledger is complete and exact through all of it, a lagging node can never make it
drop transfers, failures raise instead of returning partial lists, and it stays inside its pace.
"""
from __future__ import annotations

import os as _os_guard
_os_guard.environ.setdefault("PLUTUS_ETHERSCAN_DISABLE", "1")
_os_guard.environ["PLUTUS_RPC_DISABLE"] = "1"   # only the fake endpoints set below are used

import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="plutus_rpc_"))
os.environ["PLUTUS_DB"] = str(_TMP / "rpc.db")

from plutus import config  # noqa: E402

config.DB_PATH = pathlib.Path(os.environ["PLUTUS_DB"])

from plutus import db  # noqa: E402
from plutus.sources import chainrpc as R, transfers  # noqa: E402
from plutus.track import trackers as T  # noqa: E402

R.RPS = 1000.0                                   # pacing is tested on its own below
TOPIC = R.TRANSFER_TOPIC
ZERO = "0x" + "0" * 40
TOKEN = "0x" + "7a" * 20
A_URL, B_URL = "https://node-a.test/rpc", "https://node-b.test/rpc"


def _addr(n: int) -> str:
    return "0x%040x" % n


class Node:
    """A chain plus one node's view of it."""

    def __init__(self, chain, head, *, cap=40, slow_span=5_000, state_window=50, batch_max=60,
                 fail_first=0, http429=0, down=False):
        self.chain, self.head = chain, head
        self.cap, self.slow_span, self.state_window, self.batch_max = cap, slow_span, state_window, batch_max
        self.fail_first, self.http429, self.down = fail_first, http429, down
        self.requests: list = []
        self.max_span_asked = 0

    def answer(self, body):
        if self.down:
            raise R.requests.ConnectionError("connection refused")
        if self.fail_first > 0:
            self.fail_first -= 1
            return 502, {"error": "bad gateway"}
        if self.http429 > 0:
            self.http429 -= 1
            return 429, {"error": "slow down"}
        if isinstance(body, list):
            if len(body) > self.batch_max:
                return 429, {"error": "batch too large"}
            return 200, [dict(self._one(b), id=b["id"]) for b in body]
        return 200, dict(self._one(body), id=body["id"])

    def _one(self, b):
        m, p = b["method"], b["params"]
        self.requests.append(m)
        if m == "eth_blockNumber":
            return {"result": hex(self.head)}
        if m == "eth_getBlockByNumber":
            n = int(p[0], 16)
            if n > self.head:
                return {"result": None}
            return {"result": {"number": p[0], "timestamp": hex(1_700_000_000 + n // 10)}}
        if m == "eth_call":
            data, tag = p[0]["data"], p[1]
            blk = self.head if tag == "latest" else int(tag, 16)
            if blk < self.head - self.state_window:
                return {"error": {"code": -32000, "message": "historical state abc is not available"}}
            if data == "0x313ce567":
                return {"result": hex(18)}
            if data == "0x18160ddd":
                return {"result": hex(self.chain.supply_at(min(blk, self.head)))}
        if m == "eth_getLogs":
            q = p[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            self.max_span_asked = max(self.max_span_asked, hi - lo + 1)
            rows = [x for x in self.chain.logs if lo <= x["b"] <= min(hi, self.head)]  # past head: []
            if len(rows) > self.cap:
                return {"error": {"code": -32000,
                                  "message": f"logs matched by query exceeds limit of {self.cap}"}}
            if hi - lo + 1 > self.slow_span and rows:
                return {"error": {"code": -32000, "message": "log query timed out"}}
            return {"result": [self.chain.rpc_log(x) for x in rows]}
        return {"error": {"code": -32601, "message": "method not found"}}


class Chain:
    def __init__(self):
        self.logs: list[dict] = []

    def add(self, block, frm, to, raw, tx=None):
        idx = sum(1 for x in self.logs if x["b"] == block)
        self.logs.append({"b": block, "i": idx, "f": frm, "t": to, "v": raw,
                          "tx": tx or "0x%064x" % (block * 1000 + idx)})

    def supply_at(self, block):
        s = 0
        for x in self.logs:
            if x["b"] <= block:
                s += x["v"] if x["f"] == ZERO else 0
                s -= x["v"] if x["t"] == ZERO else 0
        return s

    def rpc_log(self, x):
        return {"address": TOKEN, "topics": [TOPIC, "0x" + "0" * 24 + x["f"][2:], "0x" + "0" * 24 + x["t"][2:]],
                "data": "0x%064x" % x["v"], "blockNumber": hex(x["b"]), "transactionHash": x["tx"],
                "logIndex": hex(x["i"]), "blockTimestamp": "0x0", "removed": False}


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.ok = code == 200

    def json(self):
        return self._b


def _install(nodes: dict[str, Node]):
    os.environ["PLUTUS_RPC_DISABLE"] = ""
    os.environ["PLUTUS_RPC_ROBINHOOD"] = ",".join(nodes)
    R._span.clear()

    def post(url, json=None, timeout=None):
        code, body = nodes[url].answer(json)
        return Resp(code, body)
    R.requests.post = post


def _uninstall():
    os.environ["PLUTUS_RPC_DISABLE"] = "1"
    os.environ.pop("PLUTUS_RPC_ROBINHOOD", None)


def _busy_chain(n_transfers=900, span=200_000, holders=60):
    ch = Chain()
    ch.add(10, ZERO, _addr(1), 10 ** 27)                      # the mint: beyond float precision
    for k in range(n_transfers):
        b = 11 + (k * 7919) % span
        ch.add(b, _addr(1 + k % holders), _addr(1 + (k * 31 + 7) % holders), 10 ** 18 + k)
    ch.logs.sort(key=lambda x: (x["b"], x["i"]))
    return ch


def _check_logs(got, ch, lo, hi):
    want = {(x["tx"], x["i"]) for x in ch.logs if lo <= x["b"] <= hi}
    have = {(g["tx_hash"], g["log_index"]) for g in got}
    assert have == want, f"missing {len(want - have)}, extra {len(have - want)}"
    assert len(got) == len(have), "duplicates returned"
    assert got == sorted(got, key=lambda g: (g["block"], g["log_index"])), "not in chain order"


# ── the log reader ──────────────────────────────────────────────────────────────────
def test_caps_and_timeouts_shrink_the_range_and_nothing_is_lost():
    ch = _busy_chain()
    node = Node(ch, 250_000)
    _install({A_URL: node})
    try:
        got = R.transfer_logs("robinhood", TOKEN, 0, 240_000)
    finally:
        _uninstall()
    _check_logs(got, ch, 0, 240_000)
    assert all(g["ts"] > 0 for g in got), "block times were not filled in"


def test_a_node_behind_the_range_is_never_trusted_with_it():
    """A node that is behind answers [] for blocks it lacks -- silently losing transfers."""
    ch = _busy_chain()
    behind, ahead = Node(ch, 150_000), Node(ch, 250_000)
    _install({A_URL: behind, B_URL: ahead})
    try:
        got = R.transfer_logs("robinhood", TOKEN, 0, 240_000)
    finally:
        _uninstall()
    _check_logs(got, ch, 0, 240_000)
    assert "eth_getLogs" not in behind.requests, "a lagging node was asked for logs it cannot have"
    assert all(abs(g["ts"] - (1_700_000_000 + g["block"] // 10)) <= 1 for g in got),         "block times the lagging node lacked were wrong -- not asked of the next node"


def test_if_every_node_is_behind_it_raises_instead_of_returning_less():
    ch = _busy_chain()
    _install({A_URL: Node(ch, 150_000)})
    try:
        R.transfer_logs("robinhood", TOKEN, 0, 240_000)
        raise AssertionError("returned a partial history instead of failing")
    except R.Unreachable:
        pass
    finally:
        _uninstall()


def test_a_dead_node_fails_over_mid_history():
    ch = _busy_chain()
    a, b = Node(ch, 250_000), Node(ch, 250_000)
    _install({A_URL: a, B_URL: b})
    try:
        orig = a.answer
        calls = [0]

        def dies_later(body):
            calls[0] += 1
            if calls[0] > 4:
                a.down = True
            return orig(body)
        a.answer = dies_later
        slept = []
        real_sleep = R.time.sleep
        R.time.sleep = lambda s: slept.append(s)
        try:
            got = R.transfer_logs("robinhood", TOKEN, 0, 240_000)
        finally:
            R.time.sleep = real_sleep
    finally:
        _uninstall()
    _check_logs(got, ch, 0, 240_000)
    assert "eth_getLogs" in b.requests, "the second node never took over"


def test_5xx_and_429_are_retried_and_pause_every_caller():
    ch = _busy_chain(n_transfers=50)
    node = Node(ch, 250_000, fail_first=1, http429=1)
    _install({A_URL: node})
    real_sleep = R.time.sleep
    R.time.sleep = lambda s: None
    try:
        assert R.latest_block("robinhood") == 250_000 - config.chain("robinhood").confirm_blocks
        row = R._db().execute("SELECT next_free FROM rate_state WHERE k=?",
                              ("rpc:node-a.test",)).fetchone()
        assert row and row[0] > time.time(), "a 429 did not hold the shared pacer"
    finally:
        R.time.sleep = real_sleep
        R._db().execute("DELETE FROM rate_state WHERE k='rpc:node-a.test'")
        _uninstall()


def test_an_unknown_node_error_raises_and_does_not_loop():
    ch = _busy_chain(n_transfers=5)
    node = Node(ch, 1000)
    node._one = lambda b: {"error": {"code": -32602, "message": "invalid argument 0"}}
    _install({A_URL: node})
    try:
        R.transfer_logs("robinhood", TOKEN, 0, 900)
        raise AssertionError("an unexplained node error was swallowed")
    except R.RpcError as exc:
        assert not isinstance(exc, R.RangeTooLarge)
    finally:
        _uninstall()
    assert len(node.requests) == 0 or len(node.requests) < 5


def test_one_block_over_the_cap_is_reported_not_looped_on():
    ch = Chain()
    for k in range(50):
        ch.add(100, ZERO, _addr(k + 1), 1)
    _install({A_URL: Node(ch, 1000, cap=40)})
    try:
        R.transfer_logs("robinhood", TOKEN, 0, 900)
        raise AssertionError("a single over-full block did not raise")
    except R.RpcError as exc:
        assert "alone holds" in str(exc)
    finally:
        _uninstall()


def test_batched_block_times_shrink_when_refused():
    ch = _busy_chain(n_transfers=60)                        # few blocks: every time fetched exactly
    node = Node(ch, 250_000, batch_max=7)
    _install({A_URL: node})
    real_sleep = R.time.sleep
    R.time.sleep = lambda s: None
    try:
        got = R.transfer_logs("robinhood", TOKEN, 0, 240_000)
    finally:
        R.time.sleep = real_sleep
        R._db().execute("DELETE FROM rate_state WHERE k='rpc:node-a.test'")
        _uninstall()
    assert all(g["ts"] == 1_700_000_000 + g["block"] // 10 for g in got), "wrong block times"


def test_a_backfill_fetches_anchor_times_not_every_block():
    """Thousands of distinct blocks at the node's ~10 items/s would take half an hour."""
    ch = _busy_chain(n_transfers=3000, span=2_000_000)
    node = Node(ch, 2_100_000, cap=5000, slow_span=10 ** 9)
    _install({A_URL: node})
    try:
        got = R.transfer_logs("robinhood", TOKEN, 0, 2_050_000)
    finally:
        _uninstall()
    fetched = node.requests.count("eth_getBlockByNumber")
    blocks = len({g["block"] for g in got})
    gap = config.chain("robinhood").time_anchor_blocks
    assert blocks > 1000 and fetched <= 2_000_000 // gap + 3,         f"fetched {fetched} block times for {blocks} blocks"
    worst = max(abs(g["ts"] - (1_700_000_000 + g["block"] // 10)) for g in got)
    assert worst <= 1, f"interpolated times off by {worst}s"


def test_supply_is_read_at_the_synced_block_and_old_state_is_reported():
    ch = _busy_chain(n_transfers=10)
    ch.add(260_000 - 20, ZERO, _addr(5), 777)                  # a mint AFTER the synced block
    _install({A_URL: Node(ch, 260_000, state_window=50)})
    try:
        assert R.token_supply("robinhood", TOKEN, 260_000 - 40) == 10 ** 27
        assert R.token_supply("robinhood", TOKEN) == 10 ** 27 + 777
        try:
            R.token_supply("robinhood", TOKEN, 1000)
            raise AssertionError("pruned state was not reported")
        except R.StateUnavailable:
            pass
    finally:
        _uninstall()


def test_the_free_rpc_is_preferred_and_etherscan_is_only_a_fallback():
    _install({A_URL: Node(Chain(), 10)})
    try:
        assert isinstance(transfers.for_chain("robinhood"), transfers.RpcSource)
    finally:
        _uninstall()
    assert transfers.for_chain("robinhood") is None, "with no RPC and no key there is no source"
    assert R.endpoints("robinhood") == [], "the test guard did not disable real endpoints"


def test_the_default_robinhood_endpoint_is_the_measured_public_one():
    os.environ["PLUTUS_RPC_DISABLE"] = ""
    try:
        eps = R.endpoints("robinhood")
    finally:
        os.environ["PLUTUS_RPC_DISABLE"] = "1"
    assert [e.url for e in eps] == ["https://rpc.mainnet.chain.robinhood.com"]


def test_the_pacer_spaces_calls_across_threads():
    import threading
    R.RPS = 10.0
    R._db().execute("DELETE FROM rate_state WHERE k='rpc:pace.test'")
    ep = R.Endpoint("https://pace.test/")
    stamps = []

    def go():
        R._pace(ep)
        stamps.append(time.time())
    try:
        th = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in th]
        [t.join() for t in th]
    finally:
        R.RPS = 1000.0
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.09, f"calls were not spaced: {gaps}"


# ── the ledger on top of it ─────────────────────────────────────────────────────────
def test_track_ledger_on_the_rpc_is_exact_then_incremental():
    ch = _busy_chain()
    node = Node(ch, 250_000, state_window=500)
    _install({A_URL: node})
    try:
        tid = db.upsert_token("robinhood", TOKEN, symbol="RPC", supply_nominal=1e9)
        T.clear_abort(tid)
        r = T.track_ledger(tid)
        assert r.ok and "reconciles to total supply exactly" in r.detail, r.detail
        assert r.calls > 0
        assert db.ledger_healthy(tid)
        synced = db.ledger_state(tid)["synced_block"]
        assert synced == 250_000 - config.chain("robinhood").confirm_blocks
        # the chain moves on: a transfer and a burn
        ch.add(250_050, _addr(3), _addr(99), 5)
        ch.add(250_060, _addr(4), ZERO, 10 ** 18)
        node.head = 250_400
        n_before = R.calls()
        r = T.track_ledger(tid)
        assert r.ok and "2 new transfer(s)" in r.detail, r.detail
        # head, the serving node's head, one getLogs, one batch of block times
        assert R.calls() - n_before <= 4, f"an incremental sync took {R.calls() - n_before} calls"
        assert db.holdings_sum_raw(tid) == ch.supply_at(250_300)
        bal = {r_["address"]: int(r_["raw"]) for r_ in db.connect().execute(
            "SELECT address, raw FROM holdings WHERE token_id=?", (tid,))}
        truth = {}
        for x in ch.logs:
            if x["b"] <= 250_300:
                truth[x["f"]] = truth.get(x["f"], 0) - x["v"]
                truth[x["t"]] = truth.get(x["t"], 0) + x["v"]
        truth = {a: v for a, v in truth.items() if a != ZERO and v}
        assert {a: v for a, v in bal.items() if v} == truth, "a balance differs from the chain"
    finally:
        _uninstall()


def test_a_supply_check_that_cannot_run_defers_and_later_ends_exact_mode():
    ch = _busy_chain(n_transfers=20)
    node = Node(ch, 250_000, state_window=500)
    _install({A_URL: node})
    try:
        tok = "0x" + "7b" * 20
        tid = db.upsert_token("robinhood", tok, symbol="RPC2", supply_nominal=1e9)
        T.clear_abort(tid)
        # same logs, different token address: the fake node ignores the address filter
        assert T.track_ledger(tid).ok and db.ledger_healthy(tid)
        node.state_window = 0                                 # state now always "gone"
        node.head += 150
        db.connect().execute("UPDATE ledger_state SET verified_ts=? WHERE token_id=?",
                             (db.now() - T.LEDGER_VERIFY_S - 1, tid))
        db.connect().commit()
        r = T.track_ledger(tid)
        assert r.ok and "deferred" in r.detail, r.detail
        assert db.ledger_state(tid) is not None, "a check that could not RUN rebuilt the ledger"
        db.connect().execute("UPDATE ledger_state SET verified_ts=? WHERE token_id=?",
                             (db.now() - db.LEDGER_VERIFIED_S - 1, tid))
        db.connect().commit()
        assert not db.ledger_healthy(tid), "exact mode survived hours without a supply check"
    finally:
        _uninstall()


def test_a_dead_rpc_fails_the_tick_without_touching_the_ledger():
    ch = _busy_chain(n_transfers=20)
    node = Node(ch, 250_000, state_window=500)
    _install({A_URL: node})
    real_sleep = R.time.sleep
    R.time.sleep = lambda s: None
    try:
        tok = "0x" + "7c" * 20
        tid = db.upsert_token("robinhood", tok, symbol="RPC3", supply_nominal=1e9)
        T.clear_abort(tid)
        assert T.track_ledger(tid).ok
        before = db.holdings_sum_raw(tid), db.ledger_state(tid)["synced_block"]
        node.down = True
        r = T.track_ledger(tid)
        assert not r.ok and r.status == "failed", r.detail
        assert (db.holdings_sum_raw(tid), db.ledger_state(tid)["synced_block"]) == before
    finally:
        R.time.sleep = real_sleep
        _uninstall()


def test_a_token_deleted_during_its_backfill_gets_nothing_written():
    ch = _busy_chain(n_transfers=50)
    node = Node(ch, 250_000, state_window=500)
    _install({A_URL: node})
    tok = "0x" + "7d" * 20
    tid = db.upsert_token("robinhood", tok, symbol="DEL", supply_nominal=1e9)
    T.clear_abort(tid)
    real = R.transfer_logs

    def slow_then_deleted(*a):
        out = real(*a)
        T.abort(tid)                                   # the operator pressed delete meanwhile
        return out
    R.transfer_logs = slow_then_deleted
    try:
        r = T.track_ledger(tid)
    finally:
        R.transfer_logs = real
        _uninstall()
    assert r.status == "skipped", r.detail
    n = db.connect().execute("SELECT COUNT(*) FROM transfers WHERE token_id=?", (tid,)).fetchone()[0]
    assert n == 0 and db.ledger_state(tid) is None, "a deleted token's backfill was written anyway"


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
            except Exception as exc:                        # noqa: BLE001
                fails += 1
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
