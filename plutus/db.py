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
  block INTEGER,                   -- chain block of the fill; lets a balance be rolled forward exactly
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

-- Does the free pool source agree with GMGN for THIS token? Measured, never assumed: on a
-- full-range pool the two agree within ~1%, on a concentrated-liquidity pool the free source's
-- derived reserves were off by 150-240%. A token reads reserves from the free source only while
-- its latest check passed.
CREATE TABLE IF NOT EXISTS pool_parity (
  token_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  r_diff REAL, q_diff REAL, spot_diff REAL, ok INTEGER NOT NULL,
  PRIMARY KEY (token_id, ts)
);

-- The trade feed returns a fixed window of recent fills. When a full window arrives that does
-- not reach back to the newest fill already stored, fills in between were never seen. Balances
-- rolled forward from fills cannot be trusted across such a gap, so it is recorded and the
-- affected wallets are re-read.
-- One full wallet read per token at a time, ACROSS PROCESSES. The web server and a CLI
-- `tick --full` are separate processes; an in-memory flag in one cannot see the other, and two
-- full reads of the same wallets is the same budget spent twice. `expires` lets a crashed
-- holder's lock lapse instead of blocking forever.
CREATE TABLE IF NOT EXISTS scan_lock (
  token_id INTEGER PRIMARY KEY, owner TEXT NOT NULL, started INTEGER, expires INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tape_gaps (
  token_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  after_block INTEGER, before_block INTEGER,
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
        _migrate(c)
        c.commit()
        _conn = c
    return _conn


def _migrate(c: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema. Additive only; never drops data."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
    if "block" not in cols:
        c.execute("ALTER TABLE trades ADD COLUMN block INTEGER")


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
    later automatic pass. An operator who classified a wallet by hand knows something the
    heuristics do not, and a sweep that overwrites them destroys exactly that."""
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
    # Named columns, so a column added later can never shift a value into the wrong field.
    rows = [tuple(r) + (None,) * (13 - len(r)) for r in rows]
    conn.executemany(
        "INSERT OR IGNORE INTO trades (token_id, tx_hash, log_idx, pool_address, ts, side, usd, "
        "tokens, price, maker, is_ours, source, block) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn.total_changes - before


# Every table keyed by token_id. Listed once, as data, and CHECKED against the live schema on
# each delete -- a table added later and not listed here would quietly survive a wipe the
# operator believes finished, which is the worst possible outcome for a delete button.
TOKEN_KEYED = ("venues", "addresses", "calibration", "pool_obs", "trades", "balances",
               "census", "census_meta", "ticks", "campaigns", "pool_parity", "tape_gaps",
               "scan_lock")


def delete_token(token_id: int) -> dict:
    """Erase one token and every observation of it. Irreversible.

    Returns the row count removed PER TABLE. A delete that reports success without saying what
    it removed is how an operator ends up believing data is gone when it is not -- so this
    reports, and it also names any token-keyed table it did not know about.
    """
    c = connect()
    live = {r["name"] for r in
            c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    removed: dict[str, int] = {}
    for t in TOKEN_KEYED:
        if t in live:
            removed[t] = c.execute(f"DELETE FROM {t} WHERE token_id=?", (token_id,)).rowcount
    removed["tokens"] = c.execute("DELETE FROM tokens WHERE id=?", (token_id,)).rowcount
    c.commit()

    # Any OTHER table carrying a token_id column that nobody listed above.
    unlisted = []
    for t in live - set(TOKEN_KEYED) - {"tokens", "sqlite_sequence"}:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({t})").fetchall()}
        if "token_id" in cols:
            unlisted.append(t)

    # Reclaim the pages. Without this the deleted rows stay readable in the file, which for a
    # button whose whole purpose is "this data is gone" would be a lie.
    vacuumed = True
    try:
        c.execute("VACUUM")
    except Exception as exc:                                     # noqa: BLE001
        vacuumed = False
        log.warning("VACUUM after delete failed: %s", exc)

    return {"removed": removed, "rows": sum(removed.values()),
            "vacuumed": vacuumed, "unlisted_tables": unlisted}


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


def latest_balance_rows(token_id: int) -> dict[str, tuple[float, int | None, int]]:
    """Most recent observation per address, with WHEN it was taken: (tokens, height, observed_ts)."""
    rows = connect().execute(
        """SELECT address, tokens, height, observed_ts FROM balances b
           WHERE token_id=? AND observed_ts=(
             SELECT MAX(observed_ts) FROM balances WHERE token_id=b.token_id AND address=b.address)""",
        (token_id,)).fetchall()
    return {r["address"]: (r["tokens"], r["height"], r["observed_ts"]) for r in rows}


# A PREFILTER, not the rule: only fills this long before the oldest read are even considered.
# The vendor's indexer runs behind the chain, so a fill can predate a read in wall-clock time and
# still be missing from it; the block comparison is what actually decides. Wide on purpose -- too
# narrow silently drops fills the indexer was slow on, too wide only costs rows scanned.
INDEX_LAG_S = 3600


def fills_after(token_id: int,
                state: dict[str, tuple[int | None, int]]) -> dict[str, tuple[float, int]]:
    """Net tokens each address gained from fills NOT reflected in its last balance read.

    `state` maps address -> (height, observed_ts) of that read. THE RULE: a fill counts when its
    block is later than the block at which the balance last changed. That is exact, and it stays
    exact when the vendor's indexer lags -- a fill it had not indexed yet has a later block than
    the balance it reported, so it is counted; a fill it had indexed moved the balance's height to
    at least its own block, so it is not. Only when a block is missing on either side does this
    fall back to comparing times.
    """
    if not state:
        return {}
    oldest = min(obs for _h, obs in state.values())
    rows = connect().execute(
        "SELECT maker, side, tokens, block, ts FROM trades WHERE token_id=? AND ts>=?",
        (token_id, oldest - INDEX_LAG_S)).fetchall()
    out: dict[str, list] = {}
    for r in rows:
        a = r["maker"]
        if a not in state or not r["tokens"]:
            continue
        height, observed = state[a]
        if r["block"] is not None and height is not None:
            counts = r["block"] > height
        else:
            counts = r["ts"] > observed
        if not counts:
            continue
        acc = out.setdefault(a, [0.0, 0])
        acc[0] += r["tokens"] if r["side"] == "buy" else -r["tokens"]
        acc[1] += 1
    return {a: (v[0], v[1]) for a, v in out.items()}


def last_trade_block(token_id: int) -> int | None:
    row = connect().execute("SELECT MAX(block) b FROM trades WHERE token_id=?",
                            (token_id,)).fetchone()
    return row["b"] if row else None


def record_tape_gap(token_id: int, after_block: int | None, before_block: int | None) -> None:
    connect().execute("INSERT OR IGNORE INTO tape_gaps VALUES (?,?,?,?)",
                      (token_id, now(), after_block, before_block))
    connect().commit()


def latest_tape_gap_ts(token_id: int) -> int | None:
    row = connect().execute("SELECT MAX(ts) t FROM tape_gaps WHERE token_id=?",
                            (token_id,)).fetchone()
    return row["t"] if row else None


def record_pool_parity(token_id: int, r_diff: float, q_diff: float, spot_diff: float,
                       ok: bool) -> None:
    connect().execute("INSERT OR REPLACE INTO pool_parity VALUES (?,?,?,?,?,?)",
                      (token_id, now(), r_diff, q_diff, spot_diff, int(ok)))
    connect().commit()


def latest_pool_parity(token_id: int) -> sqlite3.Row | None:
    return connect().execute(
        "SELECT * FROM pool_parity WHERE token_id=? ORDER BY ts DESC LIMIT 1",
        (token_id,)).fetchone()


def stalest_balance_ts(token_id: int) -> int | None:
    """When the OLDEST classified balance was last read; None if none has been read at all.

    A full reconciliation is due when this is old. Computing it from stored reads rather than an
    in-memory timer means a server restarted ten minutes after a full sweep does not redo it.
    """
    classified = {r["address"] for r in connect().execute(
        "SELECT address FROM addresses WHERE token_id=? AND class IN "
        "('ours','pool','burnt','locked','unknown')", (token_id,)).fetchall()}
    seen = [obs for a, (_t, _h, obs) in latest_balance_rows(token_id).items() if a in classified]
    # Over wallets that HAVE been read. One that never has -- a read that keeps failing, one
    # added later -- is picked up by the delta sweep; letting it make "stalest" None would make
    # a full re-read of every wallet due on every cycle.
    return min(seen) if seen else None


SCAN_LOCK_TTL_S = 1800


def _lock_conn() -> sqlite3.Connection:
    """A short-lived autocommit connection for the lock, so BEGIN IMMEDIATE is always legal."""
    c = sqlite3.connect(config.DB_PATH, timeout=10, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    c.execute("CREATE TABLE IF NOT EXISTS scan_lock (token_id INTEGER PRIMARY KEY, "
              "owner TEXT NOT NULL, started INTEGER, expires INTEGER NOT NULL)")
    return c


def acquire_scan_lock(token_id: int, owner: str, ttl: int = SCAN_LOCK_TTL_S) -> dict | None:
    """Take the full-read lock for a token. Returns None on success, else who holds it."""
    c = _lock_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        t = now()
        row = c.execute("SELECT owner, started, expires FROM scan_lock WHERE token_id=?",
                        (token_id,)).fetchone()
        if row and row["expires"] > t and row["owner"] != owner:
            c.execute("COMMIT")
            return dict(row)
        c.execute("INSERT OR REPLACE INTO scan_lock (token_id, owner, started, expires) "
                  "VALUES (?,?,?,?)", (token_id, owner, t, t + ttl))
        c.execute("COMMIT")
        return None
    finally:
        c.close()


def refresh_scan_lock(token_id: int, owner: str, ttl: int = SCAN_LOCK_TTL_S) -> None:
    c = _lock_conn()
    try:
        c.execute("UPDATE scan_lock SET expires=? WHERE token_id=? AND owner=?",
                  (now() + ttl, token_id, owner))
    finally:
        c.close()


def release_scan_lock(token_id: int, owner: str) -> None:
    c = _lock_conn()
    try:
        c.execute("DELETE FROM scan_lock WHERE token_id=? AND owner=?", (token_id, owner))
    finally:
        c.close()


def scan_lock_holder(token_id: int) -> dict | None:
    c = _lock_conn()
    try:
        row = c.execute("SELECT owner, started, expires FROM scan_lock WHERE token_id=? "
                        "AND expires>?", (token_id, now())).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def latest_census_meta(token_id: int) -> sqlite3.Row | None:
    return connect().execute("SELECT * FROM census_meta WHERE token_id=? ORDER BY sweep_ts DESC "
                             "LIMIT 1", (token_id,)).fetchone()


def census_meta_since(token_id: int, since: int) -> list[sqlite3.Row]:
    return connect().execute("SELECT * FROM census_meta WHERE token_id=? AND sweep_ts>=? "
                             "ORDER BY sweep_ts", (token_id, since)).fetchall()


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
