"""Onboarding must not lose the operator's input, leave half-made tokens, or call a deferral a failure.

Three problems found in production:
  8. Resubmitting /add rewrote the wallet file wholesale and dropped a wallet added to it by hand --
     which then counted as FLOAT, understating our position.
  9. A discovery the budget refused left a token with no name, no supply and nothing classified,
     shown on the site as a nameless token.
 10. A full pull that read 257 of 438 wallets and queued the rest showed "inventory FAILED".
"""
from __future__ import annotations

import os as _os_guard
_os_guard.environ.setdefault("PLUTUS_ETHERSCAN_DISABLE", "1")   # never real Etherscan here
_os_guard.environ.setdefault("PLUTUS_RPC_DISABLE", "1")          # never a real chain node here

import asyncio
import importlib
import os
import pathlib
import sys
import tempfile
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="plutus_ux_"))
os.environ["PLUTUS_DB"] = str(_TMP / "ux.db")

from plutus import config  # noqa: E402

config.DB_PATH = pathlib.Path(os.environ["PLUTUS_DB"])
config.BASE_DIR = _TMP                       # wallet files land here, not in the repository
config.TOKENS_DIR = _TMP / "tokens"
config.TOKENS_DIR.mkdir(exist_ok=True)

from plutus import db, onboard  # noqa: E402
from plutus.web import app, auth  # noqa: E402

auth.PATH = _TMP / "no_admin.json"           # unconfigured: loopback is the operator
CHAIN = "robinhood"
A, B, C, MANUAL = ("0x" + c * 40 for c in "abcd")
_N = [0]


def _token():
    _N[0] += 1
    return "0x%040x" % (0xABC000 + _N[0] + time.time_ns() % 1000)


def _run(coro):
    return asyncio.run(coro)


def _refusing_discover(created=True):
    def discover(cfg, calibrate_fee=True):
        if created:
            db.upsert_token(cfg.chain, cfg.address)          # what discover does first
        raise RuntimeError("background daily GMGN budget spent")
    return discover


# ── 8. the form merges ─────────────────────────────────────────────────────────────
def test_resubmitting_adds_wallets_and_never_drops_one():
    label = f"merge-{_N[0]}"
    wf = config.BASE_DIR / "wallets" / f"{label}.txt"
    wf.parent.mkdir(exist_ok=True)
    wf.write_text("\n".join([A, B, MANUAL]) + "\n", encoding="utf-8")   # MANUAL added by hand
    onboard.discover = _refusing_discover(created=False)
    r = _run(app._onboard_locked(CHAIN, _token(), label, [A, C], {}, []))
    kept = set(wf.read_text(encoding="utf-8").split())
    assert {A, B, MANUAL, C} <= kept, f"the resubmit dropped wallets: now {sorted(kept)}"
    assert r.status_code == 500


def test_resubmitting_keeps_previous_exclusions():
    label = f"excl-{_N[0]}"
    tok = _token()
    tf = config.TOKENS_DIR / f"{label}.toml"
    tf.write_text(f'[token]\nchain = "{CHAIN}"\naddress = "{tok}"\nlabel = "{label}"\n\n'
                  f'[excluded]\n"{B}" = "locked"\n\n[venues]\npools = []\n', encoding="utf-8")
    onboard.discover = _refusing_discover(created=False)
    _run(app._onboard_locked(CHAIN, tok, label, [A], {}, []))
    assert B in tf.read_text(encoding="utf-8"), "a previous exclusion was dropped by the resubmit"


def test_a_refused_second_click_does_not_touch_the_files():
    label = f"guard-{_N[0]}"
    tok = _token()
    wf = config.BASE_DIR / "wallets" / f"{label}.txt"
    wf.parent.mkdir(exist_ok=True)
    wf.write_text(MANUAL + "\n", encoding="utf-8")
    app._onboarding.add((CHAIN, tok.lower()))
    try:
        req = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"),
                                    cookies={}, headers={}, query_params={})
        r = _run(app.api_onboard(req, {"chain": CHAIN, "address": tok, "label": label,
                                       "wallets": A}))
    finally:
        app._onboarding.discard((CHAIN, tok.lower()))
    assert r.status_code == 409
    assert wf.read_text(encoding="utf-8").split() == [MANUAL], \
        "a click refused as a duplicate still rewrote the wallet file"


# ── 9. no hollow tokens ────────────────────────────────────────────────────────────
def test_a_failed_discovery_leaves_no_token_behind():
    tok = _token()
    onboard.discover = _refusing_discover(created=True)
    r = _run(app._onboard_locked(CHAIN, tok, f"fail-{_N[0]}", [A], {}, []))
    assert r.status_code == 500
    assert db.find_token(CHAIN, tok) is None, "a failed discovery left a nameless token behind"


def test_an_incomplete_discovery_leaves_no_token_behind():
    tok = _token()

    def discover(cfg, calibrate_fee=True):
        tid = db.upsert_token(cfg.chain, cfg.address)
        return types.SimpleNamespace(token_id=tid, symbol=None, supply_nominal=None, notes=[])
    onboard.discover = discover
    r = _run(app._onboard_locked(CHAIN, tok, f"incomplete-{_N[0]}", [A], {}, []))
    assert r.status_code == 502
    assert db.find_token(CHAIN, tok) is None, "a discovery with no name or supply left a token"


def test_a_failed_rediscovery_keeps_the_existing_token_intact():
    tok = _token()
    db.upsert_token(CHAIN, tok, symbol="OLD", supply_nominal=1_000_000.0)

    def discover(cfg, calibrate_fee=True):
        tid = db.upsert_token(cfg.chain, cfg.address, symbol=None, supply_nominal=None)
        return types.SimpleNamespace(token_id=tid, symbol=None, supply_nominal=None, notes=[])
    onboard.discover = discover
    _run(app._onboard_locked(CHAIN, tok, f"redo-{_N[0]}", [A], {}, []))
    row = db.find_token(CHAIN, tok)
    assert row is not None, "a failed re-discovery deleted a token that already existed"
    assert row["symbol"] == "OLD" and row["supply_nominal"] == 1_000_000.0, \
        f"a failed re-discovery blanked the token: {dict(row)}"


# ── 10. a deferral is not a failure ────────────────────────────────────────────────
def test_a_budget_deferral_reports_partial_not_failed():
    from plutus.sources import gmgn
    from plutus.track import trackers as T
    tok = _token()
    tid = db.upsert_token(CHAIN, tok, symbol="P", supply_nominal=1e9)
    for i in range(20):
        db.classify(tid, "0x%040x" % (0x7000 + i), "ours", source="operator")
    gmgn.token_balance = lambda c, w, t, fresh=False, background=False: (1.0, 1)
    gmgn.balance_cached = lambda c, w, t: False
    gmgn.budget = lambda background=False, need=1: {"left": T.BUDGET_RESERVE + 8, "ok": True,
                                                   "resumes_at": time.time() + 600}
    T.clear_abort(tid)
    r = T.track_inventory(tid, full=True)
    assert r.status == "partial" and (r.done, r.of) == (8, 20), \
        f"expected PARTIAL 8/20, got status={r.status} {r.done}/{r.of}: {r.detail}"
    assert r.resume_at, "a partial pull did not say when the rest will be read"
    importlib.reload(gmgn)


def test_the_page_shows_partial_and_deferred_as_such():
    tpl = (pathlib.Path(app.__file__).parent / "templates" / "analysis.html").read_text(
        encoding="utf-8")
    for s in ("'partial'", "'deferred'", "'skipped'", "PARTIAL", "rest queued"):
        assert s in tpl, f"the pull summary does not handle {s}"


def test_a_full_pull_says_what_it_will_cost_up_front():
    import inspect
    src = inspect.getsource(app.api_refresh)
    assert "estimate" in src and "completes_at" in src, \
        "the full pull does not estimate its cost and completion before starting"


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
