"""Admin password for anything that changes state.

ONE RULE, DELIBERATELY. Reading is open — that is the point of letting someone watch. Changing
anything needs the admin password. No usernames, no roles, no accounts: there is one operator.

THE PASSWORD IS NEVER IN THIS REPOSITORY. It lives in `data/admin.json`, which is gitignored,
as a scrypt hash with a random salt. A password committed to a public repository is public
permanently, and rotating it later does not un-publish it. The same file holds a random secret
used to sign session cookies, so sessions survive a restart but cannot be forged.

BOOTSTRAP. With no password set, a request from the machine running the server is allowed
through so a fresh install is usable immediately. Setting a password ends that: from then on
everyone logs in, including the operator. That is the simplest rule to hold in your head, and
the one that stays true behind a proxy.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from plutus import config

PATH = config.DATA_DIR / "admin.json"
SESSION_DAYS = 30

# scrypt parameters. n is the work factor; 2**14 costs a few tens of milliseconds, which is
# nothing on a login and a great deal when multiplied by a guessing loop.
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32


def _read() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_configured() -> bool:
    d = _read()
    return bool(d.get("hash") and d.get("salt"))


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)


def set_password(password: str) -> None:
    """Store a new admin password. Keeps the existing session secret so logins survive."""
    if len(password) < 6:
        raise ValueError("admin password must be at least 6 characters")
    d = _read()
    salt = secrets.token_bytes(16)
    d["salt"] = base64.b64encode(salt).decode()
    d["hash"] = base64.b64encode(_derive(password, salt)).decode()
    d.setdefault("secret", base64.b64encode(secrets.token_bytes(32)).decode())
    d["set_at"] = int(time.time())
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
    try:                                   # best effort; Windows ignores the mode
        os.chmod(PATH, 0o600)
    except OSError:
        pass


def check_password(password: str) -> bool:
    d = _read()
    if not (d.get("hash") and d.get("salt")):
        return False
    want = base64.b64decode(d["hash"])
    got = _derive(password, base64.b64decode(d["salt"]))
    return hmac.compare_digest(want, got)   # constant time, so a near-miss reveals nothing


def _secret() -> bytes:
    d = _read()
    s = d.get("secret")
    if not s:
        return b""
    return base64.b64decode(s)


def mint_session() -> str:
    """A signed `expiry.signature`. Stateless, so a restart does not log the operator out."""
    exp = int(time.time()) + SESSION_DAYS * 86400
    body = str(exp).encode()
    sig = hmac.new(_secret(), body, hashlib.sha256).digest()
    return f"{exp}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def verify_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    sec = _secret()
    if not sec:
        return False
    exp_s, _, sig_s = token.partition(".")
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    want = hmac.new(sec, exp_s.encode(), hashlib.sha256).digest()
    want_s = base64.urlsafe_b64encode(want).decode().rstrip("=")
    return hmac.compare_digest(want_s, sig_s)
