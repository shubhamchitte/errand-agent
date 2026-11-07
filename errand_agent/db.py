"""SQLite helper. One file path (or ':memory:') per database; callers own the connection."""
from __future__ import annotations

import sqlite3
from pathlib import Path

MEMORY = ":memory:"


def connect(path: str | Path = MEMORY) -> sqlite3.Connection:
    """Open a SQLite connection (rows as sqlite3.Row, autocommit-on-statement via commit helpers)."""
    p = str(path)
    if p != MEMORY:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if p != MEMORY:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection, ddl: str) -> None:
    conn.executescript(ddl)
    conn.commit()


def rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None
