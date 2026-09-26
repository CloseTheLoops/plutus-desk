"""Reading is open; changing anything needs the admin password.

WHY. The desk has no accounts and six of its endpoints are destructive or expensive: delete a
token and every observation of it, stop a running campaign, start one, spend API budget on a full
pull. Reading has to stay open -- the point is that someone can watch. So the password guards
exactly the writes.

The property that matters most is the one about a reverse proxy. "Trust 127.0.0.1" is true on a
laptop and catastrophic behind nginx, where every request arrives from 127.0.0.1 and the whole
internet would read as the operator. Once a password is set, loopback earns nothing.
"""
from __future__ import annotations

import os as _os_guard
_os_guard.environ.setdefault("PLUTUS_ETHERSCAN_DISABLE", "1")   # never real Etherscan here
_os_guard.environ.setdefault("PLUTUS_RPC_DISABLE", "1")          # never a real chain node here

import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plutus.web import app, auth  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


class _Req:
    """The little of starlette.Request the guard actually reads."""

    def __init__(self, host="127.0.0.1", session=None, headers=None):
        self.client = type("C", (), {"host": host})()
        self.cookies = {app.COOKIE: session} if session else {}
        self.headers = headers or {}
        self.query_params = {}


MUTATING = ["api_onboard", "api_classify", "api_delete_token", "api_refresh",
            "api_campaign_create", "api_campaign_stop"]


def test_every_mutating_endpoint_is_guarded():
    for name in MUTATING:
        fn = getattr(app, name, None)
        assert fn is not None, f"{name} not found — was an endpoint renamed?"
        assert "_require_operator(request)" in inspect.getsource(fn), \
            f"{name} can be called by anyone who can reach the port"


def test_reads_stay_open_to_viewers():
    """A read-only desk that reads nothing is useless."""
    for name in ("api_snapshot", "api_holders", "api_campaign_status", "api_tokens",
                 "api_whoami"):
        fn = getattr(app, name, None)
        if fn is not None:
            assert "_require_operator" not in inspect.getsource(fn), \
                f"{name} is a read and must stay open"


def test_a_valid_session_is_the_operator_and_a_forged_one_is_not(tmp=None):
    prev = auth.PATH
    auth.PATH = ROOT / "data" / "admin_test.json"
    try:
        auth.set_password("a-test-password")
        good = auth.mint_session()
        assert app._is_operator(_Req(session=good)), "a valid session was rejected"
        assert not app._is_operator(_Req(session=good[:-2] + "xy")), "a forged session passed"
        assert not app._is_operator(_Req(session="")), "an empty session passed"
        assert not app._is_operator(_Req(session="99999999999.abc")), \
            "an unsigned far-future session passed"
    finally:
        auth.PATH.unlink(missing_ok=True)
        auth.PATH = prev


def test_once_a_password_exists_loopback_earns_nothing():
    """THE reverse-proxy property. Behind nginx every request comes from 127.0.0.1."""
    prev = auth.PATH
    auth.PATH = ROOT / "data" / "admin_test.json"
    try:
        auth.set_password("a-test-password")
        for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "203.0.113.9"):
            assert not app._is_operator(_Req(host)), (
                f"{host} was treated as the operator with no session — behind a proxy that "
                f"hands operator rights to every visitor")
    finally:
        auth.PATH.unlink(missing_ok=True)
        auth.PATH = prev


def test_bootstrap_lets_a_fresh_local_install_work():
    """With no password set, the machine running the server can get started."""
    prev, prev_trust = auth.PATH, app.TRUST_LOOPBACK
    auth.PATH = ROOT / "data" / "admin_absent.json"
    app.TRUST_LOOPBACK = True
    try:
        assert not auth.is_configured()
        assert app._is_operator(_Req("127.0.0.1")), "a fresh local install is locked out"
        assert not app._is_operator(_Req("203.0.113.9")), \
            "bootstrap must never extend past this machine"
    finally:
        auth.PATH, app.TRUST_LOOPBACK = prev, prev_trust


def test_bootstrap_does_not_apply_on_a_public_bind():
    prev, prev_trust = auth.PATH, app.TRUST_LOOPBACK
    auth.PATH = ROOT / "data" / "admin_absent.json"
    app.TRUST_LOOPBACK = False
    try:
        assert not app._is_operator(_Req("127.0.0.1")), \
            "bootstrap trusted loopback while bound publicly — a proxy defeats it"
    finally:
        auth.PATH, app.TRUST_LOOPBACK = prev, prev_trust


def test_login_is_rate_limited():
    src = inspect.getsource(app.api_login)
    assert "_LOGIN_FAILS" in src and "429" in src, \
        "failed logins are not rate limited, so the password can be guessed in a loop"
    assert "check_password" in src


def test_password_is_hashed_not_stored():
    src = inspect.getsource(auth)
    assert "scrypt" in src, "the password must be stored as a slow hash"
    assert "compare_digest" in src, "comparisons must be constant time"


def test_no_password_is_committed_anywhere_in_the_repo():
    """A password in a public repository is public permanently; rotating does not un-publish it.

    NOTE ON THIS TEST'S OWN HISTORY. The first version listed the real password as a search
    needle -- so the test asserting that passwords are not committed committed the password.
    A check for a known secret is itself a disclosure of that secret. It has to match the SHAPE
    of a hardcoded credential and never a value.
    """
    import re
    import subprocess

    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True).stdout.split()
    assert "data/admin.json" not in tracked, "the admin hash file is tracked by git"

    patterns = [
        re.compile(r"""set_password\(\s*['"][^'"]{4,}['"]"""),
        re.compile(r"""PLUTUS_ADMIN_PASSWORD\s*=\s*['"][^'"]{4,}['"]"""),
        re.compile(r"""(?i)\b(admin_?password|passwd)\s*=\s*['"][^'"]{6,}['"]"""),
    ]
    allow = {"tests/test_access.py"}          # this file names the shapes in order to find them
    bad = []
    for rel in tracked:
        if rel in allow:
            continue
        f = ROOT / rel
        if not f.is_file() or f.suffix in (".png", ".ico", ".jpg", ".db"):
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx in patterns:
            for m in rx.finditer(body):
                bad.append(f"{rel}:{body[:m.start()].count(chr(10)) + 1}")
    assert not bad, f"a hardcoded credential appears in tracked files: {sorted(set(bad))}"



def test_bootstrap_trusts_proxied_loopback_by_design():
    """DELIBERATE, and the operator's decision rather than the code's.

    An unconfigured server treats anything arriving on loopback as the operator, including a
    reverse proxy forwarding a stranger. That is correct when the proxy authenticates and wrong
    when it does not, and the server cannot tell which from here -- so it warns loudly at
    startup instead of guessing. Setting an admin password ends the ambiguity, which is what
    test_once_a_password_exists_loopback_earns_nothing pins down.
    """
    prev, prev_trust = auth.PATH, app.TRUST_LOOPBACK
    auth.PATH = ROOT / "data" / "admin_absent.json"
    app.TRUST_LOOPBACK = True
    try:
        assert not auth.is_configured()
        proxied = _Req("127.0.0.1", headers={"x-forwarded-for": "203.0.113.9"})
        assert app._is_operator(proxied),             "proxied loopback lost write access on an unconfigured server — this is the "             "behaviour a gated deployment depends on"
        assert not app._is_operator(_Req("203.0.113.9")),             "a direct connection from off-box must never be trusted"
    finally:
        auth.PATH, app.TRUST_LOOPBACK = prev, prev_trust


def test_the_startup_warning_names_the_proxy_case():
    """The safety here is an informed operator, so the warning has to actually say it."""
    import inspect
    src = inspect.getsource(app._startup)
    assert "reverse proxy" in src.lower() and "setpassword" in src,         "the unconfigured-server warning does not explain the proxy case or how to fix it"


def test_rate_limit_cannot_be_evaded_by_rotating_the_forwarded_header():
    """X-Forwarded-For is attacker-controlled, so a purely per-client cap is no cap at all."""
    src = inspect.getsource(app.api_login)
    assert "_LOGIN_ALL" in src,         "there is no global login cap, so rotating X-Forwarded-For gives unlimited attempts"
    assert app._LOGIN_MAX_ALL > app._LOGIN_MAX,         "the global cap must be looser than the per-client one or normal use trips it"


def test_forwarded_headers_never_grant_trust():
    """They may bucket a rate limit; they must never decide who the operator is."""
    src = inspect.getsource(app._is_operator)
    assert "_client(" not in src,         "_is_operator uses the spoofable forwarded address; it must use _peer()"
    assert "_peer(" in src


def test_session_cookie_is_secure_behind_tls():
    src = inspect.getsource(app.api_login)
    assert "secure=" in src and "x-forwarded-proto" in src,         "the session cookie is not marked secure when TLS terminated at a proxy"
    assert "httponly=True" in src, "the session cookie must not be readable from JavaScript"


def test_an_explicit_gate_flag_restores_write_access_behind_a_proxy():
    """For deployments whose proxy already authenticates. Opt-in, never inferred."""
    prev_flag, prev_path = app.TRUST_PROXY_AUTH, auth.PATH
    auth.PATH = ROOT / "data" / "admin_absent.json"
    app.TRUST_PROXY_AUTH = True
    try:
        r = _Req("127.0.0.1", headers={"x-forwarded-for": "203.0.113.9"})
        assert app._is_operator(r),             "the explicit gate flag did not restore write access for proxied traffic"
    finally:
        app.TRUST_PROXY_AUTH, auth.PATH = prev_flag, prev_path


def test_the_gate_flag_cannot_be_turned_on_from_outside():
    """It trusts the PEER, so a direct connection from elsewhere gains nothing."""
    prev_flag, prev_path = app.TRUST_PROXY_AUTH, auth.PATH
    auth.PATH = ROOT / "data" / "admin_absent.json"
    app.TRUST_PROXY_AUTH = True
    try:
        for host in ("203.0.113.9", "10.0.0.5", ""):
            assert not app._is_operator(_Req(host)), (
                f"{host} connected directly and was trusted — the flag must only cover traffic "
                f"arriving through the local gate")
    finally:
        app.TRUST_PROXY_AUTH, auth.PATH = prev_flag, prev_path

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
