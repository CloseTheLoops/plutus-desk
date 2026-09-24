"""Every template must render, and its JavaScript must actually parse.

WHY THIS EXISTS. The operator reported "I can't click on Discover & classify". The button was
fine; a single mangled character — `split(/\\r?\\n/)` where the escape had been eaten and replaced
with a real newline — was a SyntaxError that killed the entire <script> block, so no click handler
was ever attached. Nothing logged, nothing looked broken, the page just quietly did nothing.

Python cannot see a JavaScript syntax error and a browser will not tell you unless the console is
open, so the only reliable guard is to parse the rendered script the same way the browser does.
Node is already a dependency (gmgn-cli), so `node --check` costs nothing.

This is a whole CLASS of bug, not one instance: any edit that reaches these files through a layer
of shell or string quoting can drop a backslash, and the symptom is always "the page does
nothing" rather than an error.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinja2 import Environment, FileSystemLoader  # noqa: E402

from plutus import config  # noqa: E402

TPL_DIR = Path(__file__).resolve().parent.parent / "plutus" / "web" / "templates"

CONTEXTS = {
    "add.html": dict(chains=sorted(config.CHAINS.values(),
                                   key=lambda c: (not c.verified, c.name))),
    "analysis.html": dict(token_id=1, tokens=[{"id": 1, "symbol": "TOK",
                                               "chain": "robinhood",
                                               "address": "0x" + "a" * 40}]),
    "campaign.html": dict(cid=1, token_id=1, kind="push"),
    "holders.html": dict(token_id=1, tokens=[{"id": 1, "symbol": "TOK",
                                              "chain": "robinhood",
                                              "address": "0x" + "a" * 40}]),
}


def _render(name: str) -> str:
    env = Environment(loader=FileSystemLoader(TPL_DIR))
    return env.get_template(name).render(**CONTEXTS.get(name, {}))


def _node() -> str | None:
    for exe in ("node", "node.exe"):
        try:
            subprocess.run([exe, "--version"], capture_output=True, timeout=10, check=True)
            return exe
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def test_every_template_renders():
    for tpl in sorted(TPL_DIR.glob("*.html")):
        html = _render(tpl.name)
        assert len(html) > 500, f"{tpl.name} rendered suspiciously small ({len(html)} bytes)"


def test_template_javascript_parses():
    node = _node()
    if node is None:
        print("    (node not found — skipping JS parse check)")
        return
    for tpl in sorted(TPL_DIR.glob("*.html")):
        html = _render(tpl.name)
        for i, block in enumerate(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)):
            if not block.strip():
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as fh:
                fh.write(block)
                path = fh.name
            try:
                r = subprocess.run([node, "--check", path], capture_output=True,
                                   text=True, timeout=30)
                assert r.returncode == 0, (
                    f"{tpl.name} script #{i} is not valid JavaScript:\n"
                    f"{(r.stderr or '').strip()[:600]}")
            finally:
                Path(path).unlink(missing_ok=True)


def test_no_stray_literal_newline_inside_a_regex_or_string():
    """The exact shape that broke: an unterminated literal on a line ending mid-expression."""
    for tpl in sorted(TPL_DIR.glob("*.html")):
        for n, line in enumerate(tpl.read_text(encoding="utf-8").splitlines(), 1):
            s = line.rstrip()
            assert not s.endswith("split(/"), f"{tpl.name}:{n} regex literal broken across lines"
            assert not s.endswith('replace(",", "'), f"{tpl.name}:{n} string broken across lines"


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
