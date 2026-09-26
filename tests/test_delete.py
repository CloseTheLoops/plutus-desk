"""Deleting a token must leave nothing behind, and must know about every table.

WHY THIS IS A TEST AND NOT A HAND CHECK. A delete button's whole promise is that the data is
gone. The failure mode is silent and permanent in the wrong direction: a table added later that
nobody adds to `db.TOKEN_KEYED` keeps its rows, the UI reports success, and the operator believes
a wipe finished that did not. So this asserts the promise directly — no rows anywhere, the token
row gone, no token-keyed table unaccounted for, and the symbol not recoverable from the file.
"""
from __future__ import annotations

import os as _os_guard
_os_guard.environ.setdefault("PLUTUS_ETHERSCAN_DISABLE", "1")   # never real Etherscan here

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from plutus import config  # noqa: E402

_DB = pathlib.Path(tempfile.gettempdir()) / "plutus_test_delete.db"


def _fresh():
    """A database with one token and a row in every table that carries a token_id."""
    # Close any connection a previous case left open, or Windows refuses to unlink the file.
    try:
        from plutus import db as _prev
        if getattr(_prev, "_conn", None) is not None:
            _prev._conn.close()
            _prev._conn = None
    except ImportError:
        pass
    for s in ("", "-wal", "-shm"):
        pathlib.Path(str(_DB) + s).unlink(missing_ok=True)
    config.DB_PATH = str(_DB)
    import importlib

    from plutus import db as _db
    importlib.reload(_db)
    from plutus.analyze import campaign as CP
    CP.ensure()

    tid = _db.upsert_token("robinhood", "0x" + "a" * 40, symbol="TESTTOK")
    _db.record_balances(tid, [("0x" + "b" * 40, 123.0, 1), ("0x" + "c" * 40, 456.0, 2)])
    _db.classify(tid, "0x" + "b" * 40, "ours", source="operator")
    _db.record_calibration(tid, 0.036, 0.0044, True, 1e-5, "[]")
    _db.record_pool(tid, "0x" + "d" * 40, 1e6, 5e4, 1e5, "test")
    _db.connect().execute("INSERT INTO campaigns (token_id,kind,params,started_ts,state) "
                          "VALUES (?,?,?,?,?)", (tid, "push", "{}", 0, "running"))
    _db.connect().commit()
    return _db, tid


def _counts(db, tid):
    tabs = [t for t in db.TOKEN_KEYED if db.connect().execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
    return {t: db.connect().execute(
        f"SELECT COUNT(*) c FROM {t} WHERE token_id=?", (tid,)).fetchone()["c"] for t in tabs}


def test_delete_removes_every_row():
    db, tid = _fresh()
    assert sum(_counts(db, tid).values()) > 0, "fixture wrote nothing"
    db.delete_token(tid)
    left = _counts(db, tid)
    assert not any(left.values()), f"rows survived the delete: " \
                                   f"{ {k: v for k, v in left.items() if v} }"
    assert db.token_row(tid) is None, "the token row itself survived"


def test_delete_knows_about_every_token_keyed_table():
    """The regression that matters: a new table nobody listed in TOKEN_KEYED."""
    db, tid = _fresh()
    res = db.delete_token(tid)
    assert not res["unlisted_tables"], (
        f"these tables carry a token_id but are not in db.TOKEN_KEYED, so their rows survive "
        f"a delete the operator is told succeeded: {res['unlisted_tables']}")


def test_delete_reports_what_it_removed():
    db, tid = _fresh()
    before = sum(_counts(db, tid).values())
    res = db.delete_token(tid)
    assert res["rows"] == before + 1, (
        f"reported {res['rows']} rows removed, fixture had {before} + 1 token row")
    assert res["removed"], "no per-table breakdown returned"


def _on_disk(db) -> bytes:
    """Everything SQLite holds for this database: the main file AND the write-ahead log.

    Checkpointing first matters. In WAL mode a fresh write lives in the -wal file, so reading
    only the main file reports "not present" for data that is very much still on disk -- which
    would make this test pass for the wrong reason.
    """
    db.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = b""
    for s in ("", "-wal", "-shm"):
        f = pathlib.Path(str(_DB) + s)
        if f.exists():
            blob += f.read_bytes()
    return blob


def test_deleted_data_is_not_recoverable_from_the_file():
    """VACUUM, not just DELETE. Otherwise the rows stay readable on disk."""
    db, tid = _fresh()
    assert b"TESTTOK" in _on_disk(db), "fixture symbol never hit the disk"
    res = db.delete_token(tid)
    assert res["vacuumed"], "VACUUM failed, so deleted rows remain readable on disk"
    assert b"TESTTOK" not in _on_disk(db), \
        "the symbol is STILL readable on disk after delete - VACUUM did not reclaim the pages"


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
