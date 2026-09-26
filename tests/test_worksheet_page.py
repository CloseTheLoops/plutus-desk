"""The worksheet page's Save button, driven by the page's own JavaScript under Node.

Save must unlock only on a positive answer: the scan says it is finished AND the worksheet has
something to review. Production unlocked it on a 404. Asserted here, by running the real script
from add.html against a scripted server:
  * 404s, network errors and garbage replies keep Save locked, and the page says so;
  * a finished scan with an EMPTY worksheet keeps Save locked and says why;
  * a finished scan with rows unlocks it -- including after the worksheet changed and the page
    redrew itself (it used to call a function that did not exist and never unlocked).
Skipped if Node is not installed.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "plutus" / "web" / "templates" / "add.html"

HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[1], 'utf8');
const src = html.split('<script>').pop().split('</script>')[0]
  .replace(/\{\{[^}]*\}\}/g, '0').replace(/\{%[^%]*%\}/g, '');
const scenario = JSON.parse(process.argv[2]);

const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, _html: '', value: '', disabled: false, textContent: '',
    addEventListener() {}, onclick: null,
    set innerHTML(v) { this._html = v; if (this.id === 'out') { el('save').disabled = true; } },
    get innerHTML() { return this._html; } };
  return els[id];
}
global.document = { getElementById: el, querySelectorAll: () => [], addEventListener() {} };
global.window = global; global.location = { href: '' };
let clock = 0;
global.setTimeout = (fn, ms) => { clock += ms || 0; queue.push(fn); return 0; };
const queue = [];
let statusCalls = 0;
global.fetch = async (url) => {
  const step = scenario.steps[Math.min(statusCalls, scenario.steps.length - 1)];
  if (url.startsWith('/api/refresh_status')) {
    statusCalls++;
    if (step.status === 'throw') throw new Error('network down');
    return { ok: step.status === 200, status: step.status,
             json: async () => { if (step.body === 'garbage') throw new Error('bad json'); return step.body; } };
  }
  if (url.startsWith('/api/worksheet')) {
    return { ok: true, status: 200, json: async () => step.worksheet || [] };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};
eval(src);
(async () => {
  show({ token_id: 1, discovery: { symbol: 'T', venues: 1 }, worksheet: scenario.initial || [] });
  for (let i = 0; i < scenario.ticks; i++) {
    await new Promise(r => setImmediate(r));
    const fn = queue.shift(); if (fn) fn();
    await new Promise(r => setImmediate(r));
  }
  console.log(JSON.stringify({ locked: el('save').disabled, note: el('scanNote').innerHTML,
                               statusCalls }));
})();
"""


def _run(scenario: dict) -> dict:
    out = subprocess.run(["node", "-e", HARNESS, str(TEMPLATE), json.dumps(scenario)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout.strip().splitlines()[-1])


ROW = {"address": "0x" + "ab" * 20, "share": 0.05, "class": "float", "assumed": True,
       "needs_review": True, "evidence": "holds 5.0% of supply"}
DONE = {"running": False, "scan": {"running": False, "ok": True, "detail": "ok"}}


def test_404_polls_keep_save_locked_and_say_so():
    r = _run({"steps": [{"status": 404, "body": {"detail": "Not Found"}}], "ticks": 40})
    assert r["locked"], "Save unlocked while every status poll was a 404"
    assert "cannot read the scan status" in r["note"] and "404" in r["note"], r["note"]
    assert r["statusCalls"] > 5, "the page stopped polling"


def test_network_errors_and_garbage_keep_save_locked():
    for step in ({"status": "throw"}, {"status": 200, "body": "garbage"}, {"status": 500, "body": {}}):
        r = _run({"steps": [step], "ticks": 30})
        assert r["locked"], f"Save unlocked on {step}"


def test_a_finished_scan_with_nothing_to_review_stays_locked_and_says_why():
    body = {"running": False, "scan": {"running": False, "ok": False,
                                        "detail": "background daily GMGN budget spent",
                                        "balances_problem": "read 0 of 476 wallet balances — budget"}}
    r = _run({"steps": [{"status": 200, "body": body, "worksheet": []}], "ticks": 10})
    assert r["locked"], "Save unlocked over an empty worksheet"
    assert "no balances could be read" in r["note"] and "0 of 476" in r["note"], r["note"]


def test_a_finished_scan_with_rows_unlocks_after_the_page_redraws():
    r = _run({"steps": [{"status": 200, "body": DONE, "worksheet": [ROW]}], "ticks": 20})
    assert not r["locked"], f"Save stayed locked after a good scan: {r['note']}"


def test_ledger_progress_is_shown_while_it_builds():
    body = {"running": True, "scan": {"running": True, "stage": "ledger"},
            "ledger": {"running": True, "backfill": True, "from": 0, "to": 1000, "block": 500,
                       "transfers": 1234}}
    r = _run({"steps": [{"status": 200, "body": body}], "ticks": 5})
    assert r["locked"]
    assert "transfer ledger" in r["note"] and "1,234" in r["note"] and "50%" in r["note"], r["note"]


if __name__ == "__main__":
    if not shutil.which("node"):
        print("skipped (node not installed)")
        sys.exit(0)
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
