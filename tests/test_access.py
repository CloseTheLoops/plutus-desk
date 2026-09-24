"""Anyone who can load the page must not be able to destroy what it shows.

WHY. The desk has no login. Six of its endpoints delete a token and every observation of it,
stop a running campaign, start one, or spend API budget on a full pull. That is fine while the
only reachable client is the operator on this machine. The moment the port is shared so someone
can watch a campaign, "watching" includes a delete button.

The rule: loopback is the operator, everyone else is read-only unless they present PLUTUS_KEY.
"""
from __future__ import annotations

import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plutus.web import app  # noqa: E402


class _Req:
    """The little of starlette.Request that the guard actually reads."""

    def __init__(self, host, key=None, query=None):
        self.client = type("C", (), {"host": host})()
        self.headers = {"X-Plutus-Key": key} if key else {}
        self.query_params = query or {}


MUTATING = ["api_onboard", "api_classify", "api_delete_token", "api_refresh",
            "api_campaign_create", "api_campaign_stop"]


def test_every_mutating_endpoint_is_guarded():
    for name in MUTATING:
        fn = getattr(app, name, None)
        assert fn is not None, f"{name} not found — did an endpoint get renamed?"
        src = inspect.getsource(fn)
        assert "_require_operator(request)" in src, \
            f"{name} can be called by anyone who can reach the port"


def test_read_endpoints_are_not_guarded():
    """A viewer must still be able to watch. A read-only desk that reads nothing is useless."""
    for name in ("api_snapshot", "api_holders", "api_campaign_status", "api_tokens"):
        fn = getattr(app, name, None)
        if fn is None:
            continue
        assert "_require_operator" not in inspect.getsource(fn), \
            f"{name} is a read and must stay open to viewers"


def test_loopback_is_the_operator():
    for host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
        assert app._is_operator(_Req(host)), f"{host} should be treated as the operator"


def test_a_remote_viewer_is_not_the_operator():
    for host in ("192.168.1.50", "10.0.0.7", "203.0.113.9", ""):
        assert not app._is_operator(_Req(host)), f"{host} must not get operator rights"


def test_remote_with_the_right_key_is_the_operator():
    app.OPERATOR_KEY = "s3cret-value"
    try:
        assert app._is_operator(_Req("192.168.1.50", key="s3cret-value"))
        assert app._is_operator(_Req("192.168.1.50", query={"k": "s3cret-value"}))
        assert not app._is_operator(_Req("192.168.1.50", key="s3cret-valu")), \
            "a near-miss key was accepted"
        assert not app._is_operator(_Req("192.168.1.50", key="")), "an empty key was accepted"
    finally:
        app.OPERATOR_KEY = ""


def test_no_key_configured_means_no_remote_control():
    """With PLUTUS_KEY unset, a remote client can never mutate, whatever it sends."""
    app.OPERATOR_KEY = ""
    assert not app._is_operator(_Req("192.168.1.50", key="anything"))
    assert not app._is_operator(_Req("192.168.1.50", query={"k": "anything"}))


def test_the_guard_refuses_rather_than_silently_ignoring():
    import fastapi
    try:
        app._require_operator(_Req("192.168.1.50"))
        raise AssertionError("_require_operator let a remote caller through")
    except fastapi.HTTPException as exc:
        assert exc.status_code == 403, f"expected 403, got {exc.status_code}"
        assert "read-only" in str(exc.detail).lower()


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
