"""SQLite store. Token-keyed from the first table.

DOCTRINE (inherited, and each clause was paid for somewhere):
- **Append-only where it is evidence.** Observations are never updated in place. What we saw, and
  when we saw it, survives a vendor revising its own history.
- **First observation wins.** `INSERT OR IGNORE` on observation tables, so a later re-fetch can
  never rewrite the number a decision was made on.
- **WAL.** Readers (the web page) must never block the writer (the trackers).
- **Provenance on every row.** `observed_ts`, and `height`/`source` where the vendor offers them.
  A number you cannot date is a number you cannot trust.
- **`token_id` on every table.** Running one token costs nothing extra; retrofitting this later
  would touch every query.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from plutus import config

log = config.get_logger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  id INTEGER PRIMARY KEY,
  chain TEXT NOT NULL, address TEXT NOT NULL,
  symbol TEXT, name TEXT, decimals INTEGER,
  supply_nominal REAL,            -- as reported by the vendor
  supply_burnt REAL DEFAULT 0,    -- sum of addresses classed 'burnt' — LEAVES the denominator
  launchpad TEXT, launch_status TEXT, launched_ts INTEGER,
  quote_token TEXT, quote_symbol TEXT, quote_is_stable INTEGER DEFAULT 0,
  primary_venue TEXT,
  added_ts INTEGER NOT NULL,
  UNIQUE(chain, address)
);

-- every trading venue found, not just the one that matters. A liquidity migration is then
-- visible the day it happens instead of being discovered by a wrong number.
CREATE TABLE IF NOT EXISTS venues (
  token_id INTEGER NOT NULL, pool_address TEXT NOT NULL,
  dex TEXT, quote_token TEXT, quote_symbol TEXT,
  reserve_usd REAL, vol24 REAL, created_ts INTEGER,
  is_primary INTEGER DEFAULT 0, last_seen INTEGER NOT NULL,
  PRIMARY KEY (token_id, pool_address)
);

-- the classification worksheet. FLOAT IS NEVER STORED: it is the residual, computed in
-- analyze/ledger.py, which is the only thing that forces the ledger to reconcile.
CREATE TABLE IF NOT EXISTS addresses (
  token_id INTEGER NOT NULL, address TEXT NOT NULL,
  class TEXT NOT NULL,             -- ours | pool | burnt | locked | unknown
  label TEXT,
  source TEXT NOT NULL,            -- auto:gt_venue | auto:addr_type | auto:launchpad | operator | ...
  confirmed INTEGER DEFAULT 0,     -- operator has approved this classification
  evidence TEXT,
  updated_ts INTEGER NOT NULL,
  PRIMARY KEY (token_id, address)
);

-- per-token execution cost, measured from a quote ladder. NEVER a constant in code.
CREATE TABLE IF NOT EXISTS calibration (
  token_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  fee_pct REAL,                    -- multiplicative, fitted across sizes
  fee_spread REAL,                 -- max-min across the ladder; large = not a constant fee
  k_stable INTEGER,                -- did x*y=k hold across observations?
  spot REAL, samples TEXT,
  PRIMARY KEY (token_id, ts)
);

CREATE TABLE IF NOT EXISTS pool_obs (
  token_id INTEGER NOT NULL, pool_address TEXT NOT NULL, ts INTEGER NOT NULL,
  base_reserve REAL, quote_reserve REAL, price REAL, liquidity_usd REAL, source TEXT,
  PRIMARY KEY (token_id, pool_address, ts)
) WITHOUT ROWID;

-- is_ours is decided ON INGEST, never at display time. Any flow number computed on unsplit
-- tape is a bug: on the first token measured, about a fifth of volume was the operator's own wallets
-- and the raw chart read almost twice the real organic demand.
CREATE TABLE IF NOT EXISTS trades (
  token_id INTEGER NOT NULL, tx_hash TEXT NOT NULL, log_idx INTEGER DEFAULT 0,
  pool_address TEXT, ts INTEGER NOT NULL,
  side TEXT, usd REAL, tokens REAL, price REAL,
  maker TEXT, is_ours INTEGER DEFAULT 0, source TEXT,
  PRIMARY KEY (token_id, tx_hash, log_idx)
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(token_id, ts);

-- our own holdings: DIRECT per-wallet queries only. Never inferred from a ranked sweep --
-- on the first token, a ranked sweep saw barely a third of the operator's wallets; the direct query found all.
CREATE TABLE IF NOT EXISTS balances (
  token_id INTEGER NOT NULL, address TEXT NOT NULL, observed_ts INTEGER NOT NULL,
  tokens REAL, height INTEGER,
  PRIMARY KEY (token_id, address, observed_ts)
) WITHOUT ROWID;

-- third-party holder census: the union of many ranked slices, because each caps at 100 rows.
CREATE TABLE IF NOT EXISTS census (
  token_id INTEGER NOT NULL, sweep_ts INTEGER NOT NULL, address TEXT NOT NULL,
  balance REAL, amount_pct REAL, usd_value REAL,
  avg_cost REAL, realized_profit REAL, unrealized_profit REAL,
  start_holding_at INTEGER, last_active INTEGER,
  is_new INTEGER, is_suspicious INTEGER, transfer_in INTEGER,
  tags TEXT, addr_type INTEGER,
  PRIMARY KEY (token_id, sweep_ts, address)
) WITHOUT ROWID;

-- coverage is stated, never assumed. A wallet missing from a sweep is UNCOVERED, not absent.
CREATE TABLE IF NOT EXISTS census_meta (
  token_id INTEGER NOT NULL, sweep_ts INTEGER NOT NULL,
  slices INTEGER, rows_found INTEGER, holder_count INTEGER, calls INTEGER, seconds REAL,
  PRIMARY KEY (token_id, sweep_ts)
);

CREATE TABLE IF NOT EXISTS ticks (
  token_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  regime TEXT, reasoning TEXT, snapshot TEXT,
  PRIMARY KEY (token_id, ts)
);
"""

_conn: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        c = sqlite3.connect(config.DB_PATH, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=10000")
        c.executescript(SCHEMA)
        c.commit()
        _conn = c
    return _conn


def now() -> int:
    return int(time.time())


# ── token registry ────────────────────────────────────────────────────────────
def upsert_token(chain: str, address: str, **fields: Any) -> int:
    conn = connect()
    row = conn.execute("SELECT id FROM tokens WHERE chain=? AND address=?", (chain, address)).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO tokens (chain, address, added_ts) VALUES (?,?,?)",
                           (chain, address, now()))
        tid = cur.lastrowid
    else:
        tid = row["id"]
    if fields:
        cols = [k for k, v in fields.items() if v is not None]
        if cols:
            conn.execute(f"UPDATE tokens SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
                         [fields[c] for c in cols] + [tid])
    conn.commit()
    return tid


def token_row(token_id: int) -> sqlite3.Row | None:
    return connect().execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()


def find_token(chain: str, address: str) -> sqlite3.Row | None:
    return connect().execute("SELECT * FROM tokens WHERE chain=? AND address=?",
                             (chain, address)).fetchone()


def all_tokens() -> list[sqlite3.Row]:
    return connect().execute("SELECT * FROM tokens ORDER BY added_ts").fetchall()


# ── classification ────────────────────────────────────────────────────────────
def classify(token_id: int, address: str, cls: str, source: str,
             label: str | None = None, evidence: str | None = None,
             confirmed: bool = False) -> None:
    """Set an address's class. An operator decision (source='operator') is never overwritten by a
    later automatic pass — the same rule Apollo applies to operator-sourced wallet reputation."""
    conn = connect()
    prev = conn.execute("SELECT source, confirmed FROM addresses WHERE token_id=? AND address=?",
                        (token_id, address)).fetchone()
    if prev and prev["source"] == "operator" and source != "operator":
        return
    conn.execute(
        """INSERT INTO addresses (token_id,address,class,label,source,confirmed,evidence,updated_ts)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(token_id,address) DO UPDATE SET
             class=excluded.class, label=COALESCE(excluded.label,addresses.label),
             source=excluded.source, confirmed=excluded.confirmed,
             evidence=COALESCE(excluded.evidence,addresses.evidence), updated_ts=excluded.updated_ts""",
        (token_id, address, cls, label, source, int(confirmed), evidence, now()))
    conn.commit()


def classified(token_id: int, cls: str | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM addresses WHERE token_id=?"
    a: list[Any] = [token_id]
    if cls:
        q += " AND class=?"
        a.append(cls)
    return connect().execute(q, a).fetchall()


def class_map(token_id: int) -> dict[str, str]:
    return {r["address"]: r["class"] for r in classified(token_id)}


# ── observations ──────────────────────────────────────────────────────────────
def record_pool(token_id: int, pool: str, base: float, quote: float,
                liq: float | None, source: str) -> None:
    price = (quote / base) if base else None
    connect().execute(
        "INSERT OR IGNORE INTO pool_obs VALUES (?,?,?,?,?,?,?,?)",
        (token_id, pool, now(), base, quote, price, liq, source))
    connect().commit()


def latest_pool(token_id: int, pool: str | None = None) -> sqlite3.Row | None:
    q = "SELECT * FROM pool_obs WHERE token_id=?"
    a: list[Any] = [token_id]
    if pool:
        q += " AND pool_address=?"
        a.append(pool)
    return connect().execute(q + " ORDER BY ts DESC LIMIT 1", a).fetchone()


def record_trades(rows: list[tuple]) -> int:
    if not rows:
        return 0
    conn = connect()
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn.total_changes - before


def record_balances(token_id: int, rows: list[tuple[str, float, int | None]]) -> None:
    ts = now()
    connect().executemany(
        "INSERT OR IGNORE INTO balances VALUES (?,?,?,?,?)",
        [(token_id, a, ts, tok, h) for a, tok, h in rows])
    connect().commit()


def latest_balances(token_id: int) -> dict[str, tuple[float, int | None]]:
    """Most recent observation per address."""
    rows = connect().execute(
        """SELECT address, tokens, height FROM balances b
           WHERE token_id=? AND observed_ts=(
             SELECT MAX(observed_ts) FROM balances WHERE token_id=b.token_id AND address=b.address)""",
        (token_id,)).fetchall()
    return {r["address"]: (r["tokens"], r["height"]) for r in rows}


def latest_census_ts(token_id: int) -> int | None:
    r = connect().execute("SELECT MAX(sweep_ts) t FROM census WHERE token_id=?", (token_id,)).fetchone()
    return r["t"] if r and r["t"] else None


def census_rows(token_id: int, sweep_ts: int | None = None) -> list[sqlite3.Row]:
    sweep_ts = sweep_ts or latest_census_ts(token_id)
    if not sweep_ts:
        return []
    return connect().execute("SELECT * FROM census WHERE token_id=? AND sweep_ts=?",
                             (token_id, sweep_ts)).fetchall()


def record_calibration(token_id: int, fee: float, spread: float, k_stable: bool,
                       spot: float, samples: str) -> None:
    connect().execute("INSERT OR REPLACE INTO calibration VALUES (?,?,?,?,?,?,?)",
                      (token_id, now(), fee, spread, int(k_stable), spot, samples))
    connect().commit()


def latest_calibration(token_id: int) -> sqlite3.Row | None:
    return connect().execute(
        "SELECT * FROM calibration WHERE token_id=? ORDER BY ts DESC LIMIT 1", (token_id,)).fetchone()
