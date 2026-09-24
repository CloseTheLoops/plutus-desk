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

import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plutus.web import app, auth  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


class _Req:
    """The little of starlette.Request the guard actually reads."""

    def __init__(self, host="127.0.0.1", session=None):
        self.client = type("C", (), {"host": host})()
        self.cookies = {app.COOKIE: session} if session else {}
        self.headers = {}
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
