"""GeckoTerminal — free, no API key, no quota pressure.

It carries two things GMGN does not, and both are load-bearing:

1. **The full venue list.** `/tokens/{a}/pools` returns every pool, not just the biggest. On the
   first token onboarded that meant 12 venues where the vendor named one — eleven of them dust,
   but a liquidity migration into one of them would otherwise be invisible until a number went
   wrong.

2. **`tx_from_address` on every fill.** This is what makes our own trades separable from
   everyone else's, and therefore what makes capture-rate measurable at all. Without it the tape
   is a single blended number and roughly a fifth of it is us.

Free tier is ~30 req/min, so the module self-throttles and backs off on 429.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from plutus import config

log = config.get_logger("gecko")

BASE = "https://api.geckoterminal.com/api/v2"
MIN_INTERVAL_S = 2.1
_last = 0.0


def _get(path: str, params: dict | None = None, quick: bool = False) -> dict | None:
    """quick=True: one attempt, no backoff sleep — for paths that must not block a loop."""
    global _last
    for attempt in range(3):
        wait = MIN_INTERVAL_S - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
        try:
            r = requests.get(BASE + path, params=params or {}, timeout=20,
                             headers={"Accept": "application/json", "User-Agent": "plutus"})
        except requests.RequestException as exc:
            log.warning("gecko request failed (%d): %s", attempt + 1, exc)
            continue
        if r.status_code == 404:
            return None                     # a real answer: unknown token/pool
        if r.status_code == 429:
            if quick:
                return None
            time.sleep(10 * (attempt + 1))
            continue
        if r.ok:
            return r.json()
        log.warning("gecko %s -> %d", path, r.status_code)
    return None


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def venues(network: str, token: str) -> list[dict]:
    """EVERY pool for this token, richest first. Never just the primary."""
    d = _get(f"/networks/{network}/tokens/{token}/pools")
    out = []
    for r in (d or {}).get("data") or []:
        a = r.get("attributes") or {}
        dex = ((r.get("relationships") or {}).get("dex") or {}).get("data") or {}
        out.append({
            "pool_address": a.get("address"),
            "name": a.get("name"),
            "dex": dex.get("id"),
            "reserve_usd": _f(a.get("reserve_in_usd")),
            "vol24": _f((a.get("volume_usd") or {}).get("h24")),
            "base_price_usd": _f(a.get("base_token_price_usd")),
            "created_at": a.get("pool_created_at"),
        })
    out.sort(key=lambda p: (-p["reserve_usd"], -p["vol24"]))
    return out


def trades(network: str, pool: str, quick: bool = False) -> list[dict]:
    """Recent fills. `tx_from_address` is the field the whole ours/third split hangs on."""
    d = _get(f"/networks/{network}/pools/{pool}/trades", quick=quick)
    out = []
    for r in (d or {}).get("data") or []:
        a = r.get("attributes") or {}
        kind = a.get("kind")
        usd = _f(a.get("volume_in_usd"))
        frm, to = _f(a.get("from_token_amount")), _f(a.get("to_token_amount"))
        # THE BASE TOKEN'S amount is whichever side is not the quote asset: on a buy the trader
        # RECEIVES the token, on a sell they SEND it.
        base_amt = to if kind == "buy" else frm
        # Price derived as usd/base_amount, NOT from price_to_in_usd. That field is the price of
        # whatever the trader received, so on a sell it is the STABLECOIN — ~$1.00 — and storing
        # it as the token price puts a 1.0 next to a 0.0001 in the same column. Every statistic
        # over that column (24h range, realised volatility, price rank) then reads as noise.
        price = (usd / base_amt) if base_amt else 0.0
        out.append({
            "tx_hash": a.get("tx_hash"),
            "ts": _iso(a.get("block_timestamp")),
            "side": kind,
            "usd": usd,
            "maker": (a.get("tx_from_address") or "").strip(),
            "from_amount": frm,
            "to_amount": to,
            "base_amount": base_amt,
            "price_usd": price,
        })
    return out


def _iso(s: str | None) -> int:
    if not s:
        return 0
    try:
        return int(time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone)
    except (ValueError, TypeError):
        return 0


RESOLUTIONS = {"1m": ("minute", 1, 60), "5m": ("minute", 5, 300),
               "15m": ("minute", 15, 900), "1h": ("hour", 1, 3600)}


def ohlcv(network: str, pool: str, resolution: str, ts_to: int, limit: int = 1000) -> list[dict]:
    tf = RESOLUTIONS.get(resolution)
    if not tf:
        return []
    d = _get(f"/networks/{network}/pools/{pool}/ohlcv/{tf[0]}",
             {"aggregate": tf[1], "before_timestamp": ts_to, "limit": limit, "currency": "usd"})
    rows = ((d or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    out = [{"ts": int(t), "open": o, "high": h, "low": lo, "close": c, "volume": v}
           for t, o, h, lo, c, v in rows]
    out.sort(key=lambda x: x["ts"])
    return out
