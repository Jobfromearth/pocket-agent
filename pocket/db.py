"""One SQLite file holds everything: memory, calendar, chat log.

    sqlite3 .pocket/state.db '.tables'

FTS5 ships with Python's own sqlite3, so keyword search over memory needs no
server, no embedding model, and no extra dependency.
"""

from __future__ import annotations

import sqlite3
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


def connect(home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(home / "state.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript(SCHEMA)
    return conn
