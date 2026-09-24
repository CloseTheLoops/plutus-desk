"""The analysis page. One page, live, plus a "what do you want?" box that returns costed advice.

THIS PROCESS HOLDS NO PRIVATE KEY AND CANNOT TRADE. Every source it touches is read-only
(`order quote`, `portfolio`, `token pool`, `token info`, `token traders`, GeckoTerminal), and
`sources/gmgn.py` raises on any command that would need a key. Execution is a separate process,
added later; when it exists this page will write intents for it to poll, never call it.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path

from fastapi import Body, FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from plutus import config, db
from plutus.analyze import advice as A
from plutus.analyze import composition as C
from plutus.analyze import flow as F
from plutus.analyze import campaign as CP
from plutus.analyze import distribute as D
from plutus.analyze import holders as H
from plutus.analyze import ledger as L
from plutus.analyze.curve import Pool
from plutus.track import trackers as T

log = config.get_logger("web")
app = FastAPI(title="Plutus — analysis")

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                   autoescape=select_autoescape(["html"]))

TICK_S = 60          # pool + tape
INVENTORY_S = 300    # delta inventory
CENSUS_S = 3600      # full census + full inventory reconciliation

_state: dict[int, dict] = {}

# On-demand pulls. A quick pull is one pool call plus the free tape (~2s). A full pull also
# re-runs the per-wallet inventory and the 36-call census (~2min), so it cannot block a request
# — it runs in the background and the page polls this.
_jobs: dict[int, dict] = {}


def _build_stamp() -> dict:
    """What code is this process actually running?

    A stale server quietly serving old numbers is the worst failure this tool can have: every
    figure looks plausible and none of them are current. The page shows the commit and the
    process start time so "am I looking at the new version?" is answerable at a glance instead
    of by comparing numbers and guessing.
    """
    import subprocess
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, cwd=config.BASE_DIR, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                                    text=True, cwd=config.BASE_DIR, timeout=5).stdout.strip())
    except Exception:  # noqa: BLE001 — a missing git must not stop the page
        rev, dirty = "unknown", False
    return {"rev": rev, "dirty": dirty, "started": int(time.time())}


BUILD = _build_stamp()


def _pool(token_id: int) -> Pool | None:
    row = db.latest_pool(token_id)
    if not row or not row["base_reserve"]:
        return None
    cal = db.latest_calibration(token_id)
    return Pool(row["base_reserve"], row["quote_reserve"], cal["fee_pct"] if cal else 0.0)


def snapshot(token_id: int) -> dict:
    """Everything the page renders. Pure read from stored observations."""
    tok = db.token_row(token_id)
    if tok is None:
        return {"error": f"unknown token {token_id}"}
    pool = _pool(token_id)
    led = L.build(token_id)
    # Three windows, because one is misleading. On a thin token a 15-minute window is usually
    # empty, which renders as "net $0 · QUIET" and reads like a broken panel rather than a real
    # "nothing happened". The regime is decided on the shortest window that actually has fills,
    # and the page says which one, so a quiet reading is distinguishable from a stale one.
    windows = {"15m": 900, "1h": 3600, "24h": 86400}
    flows = {k: F.measure(token_id, v) for k, v in windows.items()}
    fl = next((flows[k] for k in ("15m", "1h", "24h") if flows[k].fills >= 4), flows["24h"])
    regime_window = next((k for k in ("15m", "1h", "24h") if flows[k].fills >= 4), "24h")
    regime, action, why = F.regime(fl)
    comp = C.build(token_id, pool.spot if pool else 0.0, float_true=led.float_)
    cal = db.latest_calibration(token_id)
    cap = F.capture_rate(token_id)

    # How many holders the census actually REACHED vs how many exist. The ranked slices cap at
    # 100 rows each, so small holders are invisible: on the first token we reach ~40% of holders
    # by COUNT while accounting for 99%+ by SUPPLY. Reporting the count without the denominator
    # makes the census look complete when it is merely sufficient.
    holder_count = db.connect().execute(
        "SELECT holder_count FROM census_meta WHERE token_id=? ORDER BY sweep_ts DESC LIMIT 1",
        (token_id,)).fetchone()
    vendor_holders = int(holder_count["holder_count"]) if holder_count and holder_count["holder_count"] else None

    venues = db.connect().execute(
        "SELECT * FROM venues WHERE token_id=? ORDER BY reserve_usd DESC", (token_id,)).fetchall()
    pool_row = db.latest_pool(token_id)

    # observed sell flow, for the days-to-target estimate
    day = db.connect().execute(
        """SELECT COALESCE(SUM(tokens),0) t FROM trades
           WHERE token_id=? AND ts>=? AND side='sell' AND is_ours=0""",
        (token_id, db.now() - 86400)).fetchone()
    sell_per_day = float(day["t"] or 0)

    return {
        "token": {"id": token_id, "symbol": tok["symbol"], "chain": tok["chain"],
                  "address": tok["address"], "launchpad": tok["launchpad"],
                  "launch_status": tok["launch_status"], "quote": tok["quote_symbol"],
                  "quote_is_stable": bool(tok["quote_is_stable"])},
        "market": {
            "spot": pool.spot if pool else None,
            "fdv": pool.fdv(led.nominal) if pool else None,
            "R": pool.R if pool else None, "Q": pool.Q if pool else None,
            "k_stable": True,
            "observed_ts": pool_row["ts"] if pool_row else None,
            "venues": [dict(v) for v in venues],
            "dust_venues": sum(1 for v in venues if (v["reserve_usd"] or 0) < 50),
        },
        "fee": {"pct": cal["fee_pct"] if cal else None,
                "spread": cal["fee_spread"] if cal else None,
                "ts": cal["ts"] if cal else None},
        "ledger": {
            "nominal": led.nominal, "burnt": led.burnt, "effective": led.effective,
            "ours": led.ours, "pool": led.pool, "locked": led.locked,
            "float": led.float_, "unaccounted": led.unaccounted,
            "ours_share": led.ours_share, "float_share": led.float_share,
            "ceiling": led.float_ceiling, "float_ceiling": led.float_ceiling,
            "with_pool_half": led.with_pool(0.5), "with_pool_90": led.with_pool(0.9),
            "if_unstaked": led.if_unstaked, "rows": led.rows(),
            "ours_wallets": led.ours_wallets, "ours_holding": led.ours_holding,
            "notes": led.notes, "balance_ts": led.balance_ts,
        },
        "flow": {
            "third_buy": fl.third_buy, "third_sell": fl.third_sell, "third_net": fl.third_net,
            "our_buy": fl.our_buy, "our_sell": fl.our_sell, "raw_net": fl.raw_net,
            "our_share": fl.our_share_of_volume, "fills": fl.fills,
            "sell_trend": fl.sell_trend, "trend_basis_h": fl.trend_basis_h,
            "price_rank": fl.price_rank,
            "top_maker": fl.top_maker, "top_maker_share": fl.top_maker_share,
            "sigma": fl.sigma, "capture": cap, "notes": fl.notes,
            "regime": regime, "action": action, "why": why,
            "regime_window": regime_window,
            "windows": {k: {"third_buy": f.third_buy, "third_sell": f.third_sell,
                            "third_net": f.third_net, "our_buy": f.our_buy,
                            "our_sell": f.our_sell, "our_share": f.our_share_of_volume,
                            "fills": f.fills, "raw_net": f.raw_net}
                        for k, f in flows.items()},
        },
        "composition": {
            "holders": comp.holders, "exited": comp.exited,
            "segments": [vars(s) for s in comp.segments],
            "concentration": comp.concentration, "top": comp.top,
            "coverage": comp.coverage, "float_seen": comp.float_tokens,
            "float_true": comp.float_true, "vendor_holders": vendor_holders,
            "notes": comp.notes, "sweep_ts": comp.sweep_ts,
        },
        "sell_tokens_per_day": sell_per_day,
        "build": BUILD,
        "generated": db.now(),
    }


@app.get("/", response_class=HTMLResponse)
def index(token_id: int | None = None) -> HTMLResponse:
    toks = db.all_tokens()
    if not toks:
        return HTMLResponse(_env.get_template("add.html").render(
            chains=sorted(config.CHAINS.values(), key=lambda c: (not c.verified, c.name))))
    tid = token_id if any(t["id"] == token_id for t in toks) else toks[0]["id"]
    return HTMLResponse(_env.get_template("analysis.html").render(
        token_id=tid, tokens=[dict(t) for t in toks]))


@app.get("/add", response_class=HTMLResponse)
def add_page() -> HTMLResponse:
    return HTMLResponse(_env.get_template("add.html").render(
        chains=sorted(config.CHAINS.values(), key=lambda c: (not c.verified, c.name))))


@app.get("/api/tokens")
def api_tokens() -> JSONResponse:
    return JSONResponse([{"id": t["id"], "symbol": t["symbol"], "chain": t["chain"],
                          "address": t["address"]} for t in db.all_tokens()])


def _parse_addresses(chain: str, text: str) -> list[str]:
    """Accept whatever the operator pastes: newlines, commas, spaces, trailing # comments."""
    out: list[str] = []
    for raw in (text or "").replace(",", chr(10)).splitlines():
        tok = raw.split("#", 1)[0].strip()
        for part in tok.split():
            if config.is_address(chain, part):
                out.append(config.norm_addr(chain, part))
    return sorted(set(out))


def _parse_excluded(chain: str, text: str) -> tuple[dict[str, str], list[str]]:
    """Lines of `<address> <class>`. Returns (map, problems) — a bad line is reported, not eaten."""
    out: dict[str, str] = {}
    problems: list[str] = []
    valid = {"burnt", "locked", "pool", "ours"}
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        addr = next((p for p in parts if config.is_address(chain, p)), None)
        cls = next((p.lower() for p in parts if p.lower() in valid), None)
        if addr and cls:
            out[config.norm_addr(chain, addr)] = cls
        else:
            problems.append(line[:80])
    return out, problems


@app.post("/api/onboard")
async def api_onboard(payload: dict = Body(...)) -> JSONResponse:
    """Run discovery for a new token and start tracking it.

    The form writes a real `tokens/<label>.toml` + `wallets/<label>.txt` and then calls the SAME
    `onboard.discover()` the CLI uses. One code path, and the config stays portable and
    inspectable instead of living only in a database row.
    """
    from plutus import onboard

    chain = (payload.get("chain") or "").strip()
    address = (payload.get("address") or "").strip()
    if chain not in config.CHAINS:
        return JSONResponse({"error": f"unknown chain {chain!r}"}, status_code=400)
    if not config.is_address(chain, address):
        return JSONResponse(
            {"error": f"{address!r} is not a valid address for {chain}. "
                      f"(A Uniswap v4 pool ID is not an address — paste the TOKEN contract.)"},
            status_code=400)

    label = (payload.get("label") or "").strip().lower().replace(" ", "-") or f"token-{address[2:8]}"
    label = "".join(c for c in label if c.isalnum() or c in "-_")
    wallets = _parse_addresses(chain, payload.get("wallets", ""))
    excluded, problems = _parse_excluded(chain, payload.get("excluded", ""))

    wf = config.BASE_DIR / "wallets" / f"{label}.txt"
    wf.parent.mkdir(exist_ok=True)
    wf.write_text(chr(10).join(wallets) + (chr(10) if wallets else ""), encoding="utf-8")

    lines = ["# written by the web onboarding form", "", "[token]",
             f'chain   = "{chain}"', f'address = "{address}"', f'label   = "{label}"', "",
             "[ours]", f'wallets_file = "wallets/{label}.txt"', ""]
    if excluded:
        lines.append("[excluded]")
        lines += [f'"{a}" = "{c}"' for a, c in excluded.items()]
        lines.append("")
    lines += ["[venues]", "pools = []", ""]
    (config.TOKENS_DIR / f"{label}.toml").write_text(chr(10).join(lines), encoding="utf-8")

    cfg = config.load_token(label)
    try:
        d = await asyncio.to_thread(onboard.discover, cfg)
    except Exception as exc:  # noqa: BLE001 — surface the real reason to the form
        log.exception("onboarding failed for %s", address[:12])
        return JSONResponse({"error": f"discovery failed: {exc}"}, status_code=500)

    notes = list(d.notes)
    if problems:
        notes.append(f"{len(problems)} line(s) in the excluded box could not be read and were "
                     f"skipped: {problems[0]}…")
    if not config.chain(chain).verified:
        notes.append(f"{chain} has not been run end to end before — verify wallet attribution in "
                     f"the tape before trusting capture-rate on this token")

    asyncio.create_task(_loop(d.token_id))
    asyncio.create_task(asyncio.to_thread(T.track_inventory, d.token_id, True))

    cal = d.calibration or {}
    return JSONResponse(json.loads(json.dumps({
        "token_id": d.token_id,
        "discovery": {
            "symbol": d.symbol, "supply_nominal": d.supply_nominal,
            "holder_count": d.holder_count, "launchpad": d.launchpad,
            "launch_status": d.launch_status, "quote_symbol": d.quote_symbol,
            "quote_is_stable": d.quote_is_stable, "vault": d.vault,
            "venues": len(d.venues),
            "dust": sum(1 for v in d.venues[1:] if v["reserve_usd"] < 50),
            "fee": cal.get("fee"), "fee_spread": cal.get("spread"),
            "accurate_to": cal.get("accurate_to"), "notes": notes,
        },
        "worksheet": onboard.worksheet(d.token_id, min_share=0.002),
    }, default=_safe)))


@app.post("/api/classify")
def api_classify(payload: dict = Body(...)) -> JSONResponse:
    """Operator confirmations from the worksheet. source='operator' is never overwritten later."""
    tid = int(payload.get("token_id") or 0)
    n = 0
    for c in payload.get("changes") or []:
        addr, cls = c.get("address"), c.get("cls")
        if not addr or cls not in ("ours", "pool", "burnt", "locked", "unknown"):
            continue
        db.classify(tid, addr, cls, "operator", evidence="confirmed on the worksheet",
                    confirmed=True)
        n += 1
    return JSONResponse({"updated": n})


@app.get("/api/snapshot")
def api_snapshot(token_id: int = 1) -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(snapshot(token_id), default=_safe)))


def _safe(o):
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return str(o)


@app.get("/api/advice")
def api_advice(token_id: int = 1, intent: str = Query("acquire"),
               p1: float = 0, p2: float = 0, p3: float = 0) -> JSONResponse:
    """The 'what do you want?' box. Returns a costed plan; executes nothing."""
    pool = _pool(token_id)
    if pool is None:
        return JSONResponse({"error": "no pool observation yet"}, status_code=409)
    led = L.build(token_id)
    cal = db.latest_calibration(token_id)
    accurate_to = math.inf
    if cal and cal["samples"]:
        try:
            errs = [s for s in json.loads(cal["samples"]) if abs(s.get("pred_err", 0)) <= 0.005]
            accurate_to = max((s["usd_in"] for s in errs), default=math.inf)
        except (ValueError, TypeError):
            pass

    snap = snapshot(token_id)
    if intent == "acquire":
        adv = A.acquire(pool, led, p1 / 100.0, p2,
                        sell_tokens_per_day=snap["sell_tokens_per_day"],
                        capture=0.6, accurate_to=accurate_to)
    elif intent == "push":
        # Above the model's envelope, ask the venue instead of extrapolating a curve that is
        # known to drift optimistic with size.
        real = None
        est = pool.cost_to_push(max(1.0, p1 / pool.fdv(led.nominal))) if p1 else 0.0
        if est > accurate_to:
            tok = db.token_row(token_id)
            ours = sorted(a for a, c in db.class_map(token_id).items() if c == "ours")
            if tok["quote_token"] and ours:
                try:
                    from plutus.sources import gmgn as _g
                    q = _g.quote(tok["chain"], ours[0], tok["quote_token"], tok["address"],
                                 int(est * 1e6), slippage=50)
                    real = float(q.get("output_amount") or q.get("amount_out") or 0) / 1e18 or None
                except Exception:  # noqa: BLE001 — fall back to the model, flagged
                    log.warning("live quote for push failed; using the model")
        adv = A.push(pool, led.nominal, p1, int(p2 or 60),
                     max_impact=(p3 or 4) / 100.0, accurate_to=accurate_to,
                     real_tokens=real, float_tokens=led.float_)
        if real:
            adv.warnings.append(
                "Token count came from a LIVE QUOTE, not the curve model — at this size the "
                "model runs about 2% optimistic.")
    elif intent == "distribute":
        # p1 = participation % · p2 = sell at most, % of holdings · p3 = floor price (0 = none)
        led2 = L.build(token_id)
        cap = (p2 / 100.0) * led2.ours if p2 else 0.0
        st = D.Settings(participation=max(0.01, min(1.5, (p1 or 35) / 100.0)),
                        floor_price=p3 or 0.0, max_sell_tokens=cap)
        fl = F.measure(token_id, 900)
        sold = db.connect().execute(
            """SELECT COALESCE(SUM(tokens),0) t, COALESCE(SUM(usd),0) u FROM trades
               WHERE token_id=? AND is_ours=1 AND side='sell' AND ts>=?""",
            (token_id, db.now() - 86400)).fetchone()
        peak = db.connect().execute(
            "SELECT MAX(price) p FROM trades WHERE token_id=? AND ts>=? AND price>0",
            (token_id, db.now() - 86400)).fetchone()["p"] or pool.spot
        plan = D.decide(pool, st, inflow_usd=max(0.0, fl.third_net), price_peak=peak,
                        already_sold_tokens=sold["t"] or 0.0,
                        already_sold_usd=sold["u"] or 0.0)
        return JSONResponse(json.loads(json.dumps({
            "intent": "distribute", "plan": vars(plan),
            "preview": D.preview(pool, st),
            "dial": D.participation_curve(pool, 20_000, led2.ours),
            "settings": vars(st), "holdings": led2.ours, "spot": pool.spot,
        }, default=_safe)))
    else:
        return JSONResponse({"error": f"unknown intent {intent}"}, status_code=400)

    return JSONResponse(json.loads(json.dumps({
        "intent": adv.intent, "headline": adv.headline, "detail": adv.detail,
        "paths": [vars(p) for p in adv.paths], "numbers": adv.numbers,
        "warnings": adv.warnings, "feasible": adv.feasible, "needs_quote": adv.needs_quote,
    }, default=_safe)))


@app.delete("/api/token/{token_id}")
def api_delete_token(token_id: int, confirm: str = "") -> JSONResponse:
    """Erase one token and everything observed about it. Cannot be undone.

    `confirm` must match the token's own symbol or address. A delete button that fires on a
    single click eventually deletes the token the operator was only looking at.
    """
    t = db.token_row(token_id)
    if t is None:
        return JSONResponse({"error": f"unknown token {token_id}"}, status_code=404)
    want = {(t["symbol"] or "").strip().lower(), (t["address"] or "").strip().lower()} - {""}
    if confirm.strip().lower() not in want:
        return JSONResponse(
            {"error": f"confirm did not match — type {t['symbol'] or t['address']!r} exactly"},
            status_code=400)
    res = db.delete_token(token_id)
    log.warning("DELETED token %s (%s) — %d rows", token_id, t["symbol"], res["rows"])
    return JSONResponse({"deleted": token_id, "symbol": t["symbol"], **res})


@app.get("/holders", response_class=HTMLResponse)
def holders_page(token_id: int = 1) -> HTMLResponse:
    toks = db.all_tokens()
    tid = token_id if any(t["id"] == token_id for t in toks) else (toks[0]["id"] if toks else 1)
    return HTMLResponse(_env.get_template("holders.html").render(
        token_id=tid, tokens=[dict(t) for t in toks]))


@app.get("/api/holders")
def api_holders(token_id: int = 1, target: float = 0.0) -> JSONResponse:
    """Every third-party wallet in the float, with cost basis, dormancy and recent activity.

    `target` is a supply share (0.60); when given, the response says how many of the largest
    holders would cover what that target needs.
    """
    pool = _pool(token_id)
    led = L.build(token_id)
    spot = pool.spot if pool else 0.0
    v = H.build(token_id, spot, led.float_, led.nominal)
    need = led.needed_for(target) if target else 0.0
    return JSONResponse(json.loads(json.dumps({
        "spot": spot, "supply": led.nominal, "effective": led.effective,
        "float_true": v.float_true, "float_seen": v.float_seen, "coverage": v.coverage,
        "exited": v.exited, "sweep_ts": v.sweep_ts, "active_24h": v.active_24h,
        "ours_share": led.ours_share, "notes": v.notes,
        "target": {"share": target, "need_tokens": need,
                   **(H.reachable_by(v, need) if need else {})},
        "holders": [vars(h) for h in v.holders],
    }, default=_safe)))


@app.post("/api/refresh")
async def api_refresh(token_id: int = 1, full: bool = False) -> JSONResponse:
    """Pull fresh data now.

    quick (default): pool reserves + trade tape. Cheap enough to run on a button.
    full:            also inventory and the holder census. Minutes, so it is a background job
                     and the caller polls; starting a second one while one runs is refused
                     rather than queued, because two censuses racing would interleave writes
                     into the same sweep.
    """
    if db.token_row(token_id) is None:
        return JSONResponse({"error": f"unknown token {token_id}"}, status_code=404)

    job = _jobs.get(token_id)
    if job and job.get("running"):
        return JSONResponse({"running": True, "stage": job.get("stage"),
                             "started": job.get("started"),
                             "note": "a pull is already in progress"})

    if not full:
        res = [await asyncio.to_thread(T.track_pool, token_id),
               await asyncio.to_thread(T.track_tape, token_id)]
        _jobs[token_id] = {"running": False, "finished": db.now(), "kind": "quick",
                           "results": [vars(r) for r in res]}
        return JSONResponse({"running": False, "kind": "quick",
                             "results": [vars(r) for r in res], "finished": db.now()})

    _jobs[token_id] = {"running": True, "kind": "full", "stage": "starting",
                       "started": db.now(), "results": []}

    async def run() -> None:
        j = _jobs[token_id]
        try:
            for name, fn, args in (("pool", T.track_pool, ()), ("tape", T.track_tape, ()),
                                   ("inventory", T.track_inventory, (True,)),
                                   ("census", T.track_census, ())):
                j["stage"] = name
                r = await asyncio.to_thread(fn, token_id, *args)
                j["results"].append(vars(r))
        except Exception as exc:  # noqa: BLE001 — a failed pull must not wedge the button
            log.exception("full pull failed")
            j["error"] = str(exc)[:200]
        finally:
            j["running"] = False
            j["stage"] = "done"
            j["finished"] = db.now()

    asyncio.create_task(run())
    return JSONResponse({"running": True, "kind": "full", "stage": "starting",
                         "started": db.now()})


@app.get("/api/refresh")
def api_refresh_status(token_id: int = 1) -> JSONResponse:
    return JSONResponse(_jobs.get(token_id, {"running": False, "stage": None}))


@app.post("/api/campaign")
async def api_campaign_create(payload: dict = Body(...)) -> JSONResponse:
    """Turn an intent into something that runs and is watched."""
    tid = int(payload.get("token_id") or 1)
    kind = payload.get("kind")
    if kind not in ("acquire", "push", "distribute"):
        return JSONResponse({"error": f"unknown kind {kind!r}"}, status_code=400)
    pool = _pool(tid)
    if pool is None:
        return JSONResponse({"error": "no pool observation yet"}, status_code=409)
    led = L.build(tid)
    params = dict(payload.get("params") or {})
    minutes = int(params.get("minutes") or 0) or None
    if kind == "distribute" and params.get("sell_pct"):
        params["max_sell_tokens"] = float(params["sell_pct"]) / 100.0 * led.ours
    cid = CP.create(tid, kind, params, pool, led, minutes)
    return JSONResponse({"id": cid, "url": f"/campaign/{cid}"})


@app.get("/api/campaign/{cid}")
def api_campaign_status(cid: int) -> JSONResponse:
    c = CP.get(cid)
    if not c:
        return JSONResponse({"error": "no such campaign"}, status_code=404)
    pool = _pool(c["token_id"])
    if pool is None:
        return JSONResponse({"error": "no pool observation yet"}, status_code=409)
    st = CP.status(cid, pool, L.build(c["token_id"]))
    d = vars(st)
    d["step"] = vars(st.step)
    d["token_id"] = c["token_id"]
    return JSONResponse(json.loads(json.dumps(d, default=_safe)))


@app.post("/api/campaign/{cid}/stop")
def api_campaign_stop(cid: int) -> JSONResponse:
    CP.stop(cid)
    return JSONResponse({"stopped": cid})


@app.get("/api/campaigns")
def api_campaigns(token_id: int = 1) -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(CP.listing(token_id), default=_safe)))


@app.get("/campaign/{cid}", response_class=HTMLResponse)
def campaign_page(cid: int) -> HTMLResponse:
    c = CP.get(cid)
    if not c:
        return HTMLResponse("<p style='font:15px system-ui;padding:40px'>No such campaign. "
                            "<a href='/' style='color:#539bf5'>back</a></p>", status_code=404)
    return HTMLResponse(_env.get_template("campaign.html").render(
        cid=cid, token_id=c["token_id"], kind=c["kind"]))


@app.get("/api/worksheet")
def api_worksheet(token_id: int = 1) -> JSONResponse:
    from plutus import onboard
    return JSONResponse(json.loads(json.dumps(onboard.worksheet(token_id), default=_safe)))


async def _loop(token_id: int) -> None:
    """Background trackers. Every failure is logged and survived; one bad cycle never stops it."""
    last_inv = last_census = 0.0
    while True:
        try:
            for r in (T.track_pool(token_id), T.track_tape(token_id)):
                if not r.ok:
                    log.warning("%s: %s", r.name, r.detail)
            now = time.time()
            if now - last_census > CENSUS_S:
                await asyncio.to_thread(T.track_census, token_id)
                await asyncio.to_thread(T.track_inventory, token_id, True)
                last_census = last_inv = now
            elif now - last_inv > INVENTORY_S:
                await asyncio.to_thread(T.track_inventory, token_id, False)
                last_inv = now
        except Exception:                       # noqa: BLE001 — the loop never dies of one cycle
            log.exception("tracker cycle failed")
        await asyncio.sleep(TICK_S)


@app.on_event("startup")
async def _startup() -> None:
    for t in db.all_tokens():
        asyncio.create_task(_loop(t["id"]))
        log.info("tracking %s (%s)", t["symbol"] or t["address"][:10], t["chain"])
