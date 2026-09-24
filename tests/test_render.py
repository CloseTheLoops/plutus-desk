"""Execute each page's render() against a payload full of nulls.

WHY SYNTAX CHECKING WAS NOT ENOUGH. `node --check` proved the script parses; it cannot know that
`F.sell_trend.toFixed(2)` throws once sell_trend legitimately becomes null. One TypeError inside
render() aborts the whole function, so the page draws NOTHING — not a broken panel, a blank page
— and the server logs nothing because the failure is in the browser.

Every field the API can return as null is set to null here on purpose. If a page dereferences
one without guarding, this fails loudly instead of the operator discovering it.

Needs node, which is already a dependency via gmgn-cli. A minimal DOM stub is enough: the pages
only touch getElementById, addEventListener, querySelectorAll and fetch.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinja2 import Environment, FileSystemLoader  # noqa: E402

from plutus import config  # noqa: E402

TPL_DIR = Path(__file__).resolve().parent.parent / "plutus" / "web" / "templates"

# Every nullable field, null. This is the payload the page must survive.
SNAPSHOT = {
    "token": {"id": 1, "symbol": None, "chain": "robinhood", "address": "0x" + "a" * 40,
              "launchpad": None, "launch_status": None, "quote": None, "quote_is_stable": False},
    "market": {"spot": None, "fdv": None, "R": None, "Q": None, "k_stable": True,
               "observed_ts": None, "venues": [], "dust_venues": 0},
    "fee": {"pct": None, "spread": None, "ts": None},
    "ledger": {"nominal": 1e9, "burnt": 0, "effective": 1e9, "ours": 0, "pool": 0, "locked": 0,
               "float": 1e9, "unaccounted": 0, "ours_share": 0.0, "float_share": 1.0,
               "ceiling": 1.0, "rows": [], "ours_wallets": 0, "ours_holding": 0,
               "notes": [], "balance_ts": None},
    "flow": {"third_buy": 0.0, "third_sell": 0.0, "third_net": 0.0, "our_buy": 0.0,
             "our_sell": 0.0, "raw_net": 0.0, "our_share": 0.0, "fills": 0,
             "sell_trend": None, "trend_basis_h": 0.0, "price_rank": None,
             "top_maker": "", "top_maker_share": 0.0, "sigma": 0.0, "capture": None,
             "notes": [], "regime": "QUIET", "action": "x", "why": "y",
             "regime_window": "24h",
             "windows": {k: {"third_buy": 0, "third_sell": 0, "third_net": 0, "our_buy": 0,
                             "our_sell": 0, "our_share": 0, "fills": 0, "raw_net": 0}
                         for k in ("15m", "1h", "24h")}},
    "composition": {"holders": 0, "exited": 0, "segments": [], "concentration": {},
                    "top": [], "coverage": 1.0, "float_seen": 0, "float_true": 0,
                    "notes": [], "sweep_ts": None},
    "sell_tokens_per_day": 0.0,
    "build": {"rev": "test", "dirty": False, "started": 0},
    "generated": 0,
}

HOLDERS = {
    "spot": None, "supply": 1e9, "effective": 1e9, "float_true": 0, "float_seen": 0,
    "coverage": 1.0, "exited": 0, "sweep_ts": None, "active_24h": 0, "ours_share": 0.0,
    "notes": [], "target": {"share": 0, "need_tokens": 0},
    "holders": [{
        "address": "0x" + "c" * 40, "tokens": 1.0, "share_float": 0.0, "share_supply": 0.0,
        "usd": 0.0, "avg_cost": None, "vs_cost": None, "segment": "never bought",
        "entered_ts": 0, "held_days": None, "last_active_ts": 0, "dormant_days": None,
        "realized": 0.0, "unrealized": 0.0, "has_sold": False, "tags": [],
        "suspicious": False, "fresh": False, "transfer_in": False,
        "bought_24h": 0.0, "sold_24h": 0.0, "fills_24h": 0, "cumulative_share": 0.0,
    }],
}

STUB = """
const __els = {};
function __el(){ return {
  innerHTML:'', textContent:'', value:'', checked:false, style:{}, dataset:{}, classList:{
    add(){}, remove(){}, toggle(){return false}, contains(){return false}},
  options:[{text:'',value:'',dataset:{}}], selectedOptions:[{value:'',dataset:{}}],
  selectedIndex:0, disabled:false, step:1,
  addEventListener(){}, removeEventListener(){}, add(){}, appendChild(){}, click(){},
  querySelectorAll(){return []}, closest(){return null}, remove(){},
  // querySelector must return another element, not null: the pages reach into a container and
  // set properties on what comes back (progress fill, time marker), so a null here would throw
  // inside the STUB rather than surfacing a real fault in the page.
  querySelector(){ return __el(); },
}; }
global.document = {
  getElementById(id){ return __els[id] || (__els[id]=__el()); },
  querySelectorAll(){ return []; },
  querySelector(){ return null; },
  addEventListener(){}, createElement(){ return __el(); },
  body:{ appendChild(){}, removeChild(){} },
};
global.window = { isSecureContext:false };
global.navigator = {};
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.fetch = async () => ({ json: async () => (__PAYLOAD__) });
global.location = { href:'' };
global.Option = function(t,v){ return {text:t, value:v}; };
"""


CAMPAIGN = {
    "id": 1, "kind": "push", "state": "running", "started_ts": 0, "deadline_ts": None,
    "elapsed_s": 0, "remaining_s": None, "time_pct": None, "progress_pct": 0.0,
    "params": {}, "baseline": {}, "now": {}, "token_id": 1,
    "done": {"bought_usd": 0, "bought_tok": 0, "sold_usd": 0, "sold_tok": 0,
             "fills": 0, "avg_buy": None, "avg_sell": None},
    "step": {"action": "HOLD", "usd": 0, "tokens": 0, "reason": "", "urgency": "normal"},
    "notes": [],
    "participants": {"wallets": [], "ours": {}, "third": {}, "total_volume": 0,
                     "our_share_of_volume": 0.0, "active": 0},
}


def _render_tpl(name: str) -> str:
    env = Environment(loader=FileSystemLoader(TPL_DIR))
    ctx = dict(token_id=1, cid=1, kind="push",
               tokens=[{"id": 1, "symbol": "T", "chain": "robinhood",
                        "address": "0x" + "a" * 40}],
               chains=sorted(config.CHAINS.values(), key=lambda c: (not c.verified, c.name)))
    return env.get_template(name).render(**ctx)


def _run(name: str, payload: dict, call: str) -> None:
    html = _render_tpl(name)
    script = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))
    js = (STUB.replace("__PAYLOAD__", json.dumps(payload))
          + "\n" + script + f"\ntry{{ {call} }}catch(e){{"
          "console.error('RENDER THREW: '+(e&&e.stack||e)); process.exit(3); }\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, (
            f"{name} render() failed on a payload with nulls:\n"
            f"{(r.stderr or r.stdout).strip()[:900]}")
    finally:
        Path(path).unlink(missing_ok=True)


def test_analysis_renders_with_nulls_everywhere():
    _run("analysis.html", SNAPSHOT, "render(" + json.dumps(SNAPSHOT) + ");")


def test_holders_renders_with_nulls_everywhere():
    _run("holders.html", HOLDERS, "DATA=" + json.dumps(HOLDERS) + "; render();")


def test_campaign_renders_with_nulls_everywhere():
    _run("campaign.html", CAMPAIGN, "render(" + json.dumps(CAMPAIGN) + ");")


def test_campaign_renders_when_finished_with_no_trades():
    """The final-stats block divides by volume that may be zero."""
    c = json.loads(json.dumps(CAMPAIGN))
    c["state"] = "stopped"
    _run("campaign.html", c, "render(" + json.dumps(c) + ");")


def test_analysis_survives_a_sell_trend_of_null():
    """The exact regression: sell_trend became legitimately null and the page went blank."""
    p = json.loads(json.dumps(SNAPSHOT))
    p["flow"]["sell_trend"] = None
    p["flow"]["capture"] = None
    _run("analysis.html", p, "render(" + json.dumps(p) + ");")


if __name__ == "__main__":
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        print("  node not found — skipping")
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
