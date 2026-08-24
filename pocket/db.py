"""One SQLite file holds everything: memory, calendar, chat log.

    sqlite3 .pocket/state.db '.tables'

FTS5 ships with Python's own sqlite3, so keyword search over memory needs no
server, no embedding model, and no extra dependency.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
-- what the flagship tool produces; the deterministic eval asserts on these rows
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    start TEXT NOT NULL,                 -- ISO 8601
    "end" TEXT,
    attendees TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- semantic memory: durable facts, kept searchable by an FTS5 shadow table
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT DEFAULT 'user',          -- 'user' (told) | 'consolidation' (distilled)
    forgotten INTEGER DEFAULT 0,         -- retracted, not removed: see manage_memory
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    subject, content, content=facts, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content)
    VALUES ('delete', old.id, old.subject, old.content);
END;

-- episodic memory: dated summaries of what happened
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    happened_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    summary, content=episodes, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, summary) VALUES (new.id, new.summary);
END;

-- the team board: one row per delegated task, so `select * from tasks` IS the
-- kanban. status: pending -> running -> done, or failed / blocked
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    team TEXT NOT NULL,
    key TEXT NOT NULL,
    instruction TEXT NOT NULL,
    tools TEXT DEFAULT '',               -- the scoped tool list the worker got
    depends_on TEXT DEFAULT '',          -- comma separated task keys
    status TEXT DEFAULT 'pending',
    result TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS tasks_by_team ON tasks (team, id);

-- one row per consolidation run: what it read, what it created, and whether
-- it has been walked back. See dream.py for why a run needs a name.
CREATE TABLE IF NOT EXISTS dream_runs (
    id INTEGER PRIMARY KEY,
    sha TEXT NOT NULL UNIQUE,
    ran_at TEXT NOT NULL,
    exchanges INTEGER DEFAULT 0,
    fact_ids TEXT DEFAULT '',
    episode_id INTEGER,
    episode TEXT DEFAULT '',
    mirror_before TEXT DEFAULT '',
    restored_at TEXT
);

-- raw chat log; consolidation reads the rows it has not distilled yet
CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    consolidated INTEGER DEFAULT 0,
    meta TEXT,                           -- per-turn telemetry as JSON
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class _SerializedCursor:
    """A Cursor returned by _SerializedConnection. Fetches are serialized with
    the same lock the connection uses, so a fetch on one thread's cursor can
    never interleave with another thread's execute() on the shared connection."""

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock):
        self._cursor = cursor
        self._lock = lock

    def fetchone(self):
        with self._lock:
            return self._cursor.fetchone()

    def fetchall(self):
        with self._lock:
            return self._cursor.fetchall()

    def fetchmany(self, size: int = -1):
        with self._lock:
            return self._cursor.fetchmany(size) if size != -1 else self._cursor.fetchmany()

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _SerializedConnection:
    """Wraps the raw sqlite3.Connection so every call into it is serialized —
    see the load-bearing note in connect() below for why this exists."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, *args, **kwargs) -> _SerializedCursor:
        with self._lock:
            return _SerializedCursor(self._conn.execute(*args, **kwargs), self._lock)

    def executemany(self, *args, **kwargs) -> _SerializedCursor:
        with self._lock:
            return _SerializedCursor(self._conn.executemany(*args, **kwargs), self._lock)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def connect(home: Path) -> sqlite3.Connection:
    # A team runs its workers in parallel threads (team.py) and they all write
    # through this one connection, so three things are load-bearing:
    #   check_same_thread=False  the workers are allowed to use it at all
    #   isolation_level=None     autocommit. Python's implicit transaction is
    #                            per-connection, not per-thread: with it on, one
    #                            worker's commit() ends the transaction another
    #                            worker opened, and that worker's own commit()
    #                            dies with "no transaction is active". Every write
    #                            here is one short statement, so each committing
    #                            itself costs nothing and removes the whole race.
    #   _SerializedConnection    check_same_thread=False only lifts Python's own
    #                            guard; the sqlite3 module still isn't safe for
    #                            concurrent calls on one shared Connection — two
    #                            threads calling execute() at the same moment can
    #                            corrupt its internal cursor bookkeeping and raise
    #                            InterfaceError('bad parameter or other API
    #                            misuse'). One lock around every call turns that
    #                            race into a queue.
    # The `conn.commit()` calls elsewhere stay: they are no-ops in this mode, and
    # they keep every write path readable on its own.
    conn = sqlite3.connect(home / "state.db", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript(SCHEMA)
    add_missing_columns(conn)
    return _SerializedConnection(conn)


# `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so a
# database made by an older version keeps its old columns. This is the whole
# migration story and it is deliberately this small: add a column, never rename
# one, never drop one. A schema you can only add to is a schema you can never
# lose data to.
ADDED_COLUMNS = (("facts", "forgotten", "INTEGER DEFAULT 0"),)


def add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    added = []
    for table, column, spec in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
            added.append(f"{table}.{column}")
    return added
