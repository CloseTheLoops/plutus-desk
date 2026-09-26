"""Every URL a page calls must exist, with the method it is called with.

Found in production: the worksheet polled /api/refresh_status, but the handler was registered at
/api/refresh only. Every poll 404'd, the page read the silence as "no scan running", and unlocked
Save over an empty worksheet. Nothing checked that the URLs in the templates and the routes in
the app agree. This does -- for every fetch() in every template -- and requests the status URL
the worksheet uses, exactly as written.
"""
from __future__ import annotations

import os as _os_guard
_os_guard.environ.setdefault("PLUTUS_ETHERSCAN_DISABLE", "1")
_os_guard.environ["PLUTUS_RPC_DISABLE"] = "1"          # never a real chain node here

import os
import pathlib
import re
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="plutus_routes_"))
os.environ["PLUTUS_DB"] = str(_TMP / "routes.db")

from plutus import config  # noqa: E402

config.DB_PATH = pathlib.Path(os.environ["PLUTUS_DB"])

from starlette.routing import Match  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from plutus import db  # noqa: E402
from plutus.web import app, auth  # noqa: E402

auth.PATH = _TMP / "no_admin.json"
TEMPLATES = pathlib.Path(app.__file__).parent / "templates"
FETCH = re.compile(r"fetch\(\s*([`'\"])(/[^`'\"]*)\1")


def _calls() -> list[tuple[str, str, str, str]]:
    """(template, raw url, method, concrete path) for every fetch() in every template."""
    out = []
    for f in sorted(TEMPLATES.glob("*.html")):
        text = f.read_text(encoding="utf-8")
        for m in FETCH.finditer(text):
            raw = m.group(2)
            nxt = text.find("fetch(", m.end())
            args = text[m.end(): nxt if nxt != -1 else m.end() + 400][:400]
            mm = re.search(r"method\s*:\s*['\"](\w+)['\"]", args)
            method = mm.group(1).upper() if mm else "GET"
            path = re.sub(r"\{\{[^}]*\}\}|\$\{[^}]*\}", "1", raw.split("?")[0])
            out.append((f.name, raw, method, path))
    return out


def _routed(path: str, method: str) -> bool:
    scope = {"type": "http", "path": path, "method": method, "root_path": "",
             "query_string": b"", "headers": []}
    return any(r.matches(scope)[0] == Match.FULL for r in app.app.router.routes)


def test_every_url_a_template_fetches_is_routed_with_its_method():
    calls = _calls()
    assert len(calls) >= 10, f"found only {len(calls)} fetch() calls -- the scanner is broken"
    missing = [f"{tpl}: {method} {raw}" for tpl, raw, method, path in calls
               if not _routed(path, method)]
    assert not missing, "templates call URLs the app does not serve:\n  " + "\n  ".join(missing)


def test_the_worksheet_status_url_answers_exactly_as_the_page_calls_it():
    add = (TEMPLATES / "add.html").read_text(encoding="utf-8")
    urls = [raw for tpl, raw, method, _p in _calls()
            if tpl == "add.html" and "status" in raw]
    assert urls, "the worksheet no longer polls a status URL -- update this test"
    tid = db.upsert_token("robinhood", "0x" + "5e" * 20, symbol="RT", supply_nominal=1e9)
    c = TestClient(app.app)
    for raw in urls:
        r = c.get(raw + str(tid) if raw.endswith("=") else raw)
        assert r.status_code == 200, f"GET {raw} -> {r.status_code}"
        j = r.json()
        assert isinstance(j, dict) and "running" in j, f"GET {raw} answered {j!r}"
    assert "/api/refresh_status" in add


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
