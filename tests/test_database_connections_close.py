"""
Every `with db.get_connection() as conn:` must release the SQLite file
handles when the block ends. Plain sqlite3 connections only commit there and
stay open until the garbage collector runs, which showed up on Railway as
about 130 open handles to camellia.db and its WAL right after a restart.
"""
import gc
import sqlite3

import pytest

from camellia.database import Database


def _open_connections():
    return sum(isinstance(o, sqlite3.Connection) for o in gc.get_objects())


def test_with_block_closes_the_connection(tmp_path):
    database = Database(str(tmp_path / "close.db"))
    with database.get_connection() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_repeated_reads_do_not_pile_up_open_connections(tmp_path):
    database = Database(str(tmp_path / "pileup.db"))
    gc.collect()
    before = _open_connections()
    gc.disable()
    try:
        for _ in range(50):
            with database.get_connection() as conn:
                conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        assert _open_connections() - before < 5
    finally:
        gc.enable()


def test_with_block_still_commits_and_rolls_back(tmp_path):
    database = Database(str(tmp_path / "txn.db"))
    with database.get_connection() as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError):
        with database.get_connection() as conn:
            conn.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError("abort")
    with database.get_connection() as conn:
        assert [r[0] for r in conn.execute("SELECT v FROM t")] == [1]
