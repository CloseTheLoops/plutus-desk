"""Every HTTP route must return exactly what the CLI returns for the same call.

WHY THIS IS THE GATE. The HTTP transport exists to be faster. A faster transport that returns a
slightly different shape -- an envelope left on, a key renamed, a number as a string -- does not
fail loudly. It produces wrong balances, wrong reserves and wrong quotes, which reach the operator
as confident figures. So every route is called BOTH ways against the live API and the results are
compared field by field.

Values that legitimately move between two calls (a balance, a block height, a live price) are
compared structurally rather than exactly: same keys, same types, same nesting.

Needs network and an API key. Skips cleanly without them.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plutus.sources import gmgn  # noqa: E402

CHAIN = "robinhood"


def _shape(x, depth=0):
    """Keys and types, not values — live numbers move between two calls."""
    if depth > 4:
        return type(x).__name__
    if isinstance(x, dict):
        return {k: _shape(v, depth + 1) for k, v in sorted(x.items())}
    if isinstance(x, list):
        return [_shape(x[0], depth + 1)] if x else []
    if isinstance(x, bool):
        return "bool"
    if isinstance(x, (int, float)):
        return "num"
    return type(x).__name__


def _both(*args):
    """The same call over HTTP and over the CLI."""
    http = gmgn._http_call(tuple(args))
    prev, gmgn.USE_HTTP = gmgn.USE_HTTP, False
    try:
        cli = gmgn.call(*args, attempts=2)
    finally:
        gmgn.USE_HTTP = prev
    return http, cli


def _pick_token() -> str:
    """A live token on this chain, found without hardcoding one that may be gone."""
    import json
    import subprocess
    p = subprocess.run(["cmd.exe", "/c", "gmgn-cli", "market", "trending", "--chain", CHAIN,
                        "--interval", "24h", "--limit", "3", "--raw"],
                       capture_output=True, text=True, timeout=60, env=gmgn._env(),
                       encoding="utf-8", errors="replace")
    d = json.loads(p.stdout)
    d = d.get("data", d)
    return (d.get("rank") or d.get("list"))[0]["address"]


def _check(name, args):
    http, cli = _both(*args)
    sh, sc = _shape(http), _shape(cli)
    assert sh == sc, (
        f"{name}: HTTP and CLI disagree on shape.\n"
        f"  http: {str(sh)[:400]}\n  cli : {str(sc)[:400]}")
    return http


def test_parity_across_every_route():
    if not gmgn._api_key():
        print("  no API key — skipping"); return
    tok = _pick_token()
    wallet = (gmgn.traders(CHAIN, tok, limit=3) or [{}])[0].get("address")
    assert wallet, "could not find a live wallet to test with"

    _check("gas-price", ("gas-price", "--chain", CHAIN))
    _check("token info", ("token", "info", "--chain", CHAIN, "--address", tok))
    _check("token pool", ("token", "pool", "--chain", CHAIN, "--address", tok))
    _check("token security", ("token", "security", "--chain", CHAIN, "--address", tok))
    _check("token traders", ("token", "traders", "--chain", CHAIN, "--address", tok,
                             "--limit", "5", "--order-by", "amount_percentage"))
    _check("portfolio info", ("portfolio", "info"))
    b = _check("portfolio token-balance",
               ("portfolio", "token-balance", "--chain", CHAIN, "--wallet", wallet,
                "--token", tok))
    assert "balances" in b, f"token-balance lost its envelope handling: {str(b)[:200]}"


def test_balance_values_agree_not_just_shapes():
    """Shape parity is not enough — the number itself has to match."""
    if not gmgn._api_key():
        print("  no API key — skipping"); return
    tok = _pick_token()
    wallet = (gmgn.traders(CHAIN, tok, limit=3) or [{}])[0].get("address")

    got_http = gmgn.token_balance(CHAIN, wallet, tok)
    prev, gmgn.USE_HTTP = gmgn.USE_HTTP, False
    try:
        got_cli = gmgn.token_balance(CHAIN, wallet, tok)
    finally:
        gmgn.USE_HTTP = prev
    a, b = got_http[0], got_cli[0]
    # live wallets trade between the two reads; allow drift, reject a different magnitude
    assert a > 0 and b > 0, f"one transport returned zero: http={got_http} cli={got_cli}"
    rel = abs(a - b) / max(a, b)
    assert rel < 0.05, (
        f"balances differ by {rel:.1%} between transports (http={a:,.2f} cli={b:,.2f}) — "
        f"that is more than live drift explains")


def test_unroutable_calls_fall_through_to_the_cli():
    """A command with no HTTP route must not silently return nothing."""
    try:
        gmgn._http_call(("market", "trending", "--chain", CHAIN))
        raise AssertionError("market trending has no route but _http_call did not raise _NoRoute")
    except gmgn._NoRoute:
        pass


def test_signed_routes_have_no_http_path():
    """The HTTP transport must be structurally unable to reach a signing endpoint."""
    bad = [k for k in gmgn._ROUTES
           if k[0] in ("swap", "multi-swap") or k[:2] == ("order", "strategy")]
    assert not bad, f"signed-auth commands must never have an HTTP route: {bad}"
    for path in (v[1] for v in gmgn._ROUTES.values()):
        assert "swap" not in path and "strategy" not in path, f"route reaches {path}"


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
