"""Dream — consolidation with a version history, so memory can be walked back.

Consolidation reads the exchanges nobody has distilled yet and writes durable
facts. That is a model deciding, unsupervised, what you will be remembered as
believing. It is the one background job in this repo that can quietly make the
assistant wrong about your life, and until now it left no way to see what it had
decided or to undo it.

So every run is a version:

    what it added   the fact ids and the episode id it created, exactly — not a
                    guess reconstructed from timestamps
    what it read    how many exchanges went in, and the mirror as it stood
                    before, so a diff is a diff and not a reconstruction
    a short sha     enough to name one run at a prompt

`restore` retracts exactly the facts a run added, and nothing else. It does not
roll the database back to a snapshot, because a snapshot would also undo what
you told the assistant *since* — and it does not delete, because nothing here
deletes: a retracted fact is `forgotten = 1` and stays on the row it was always
on. Undoing a bad dream should cost you that dream and nothing else.

The prompt that guides it is yours: `.pocket/dream.md` is written on first run
and read on every one after, so "what counts as worth remembering" is a file you
can edit rather than a constant you have to fork the repo to change.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

LEDGER = """\
CREATE TABLE IF NOT EXISTS dream_runs (
    id INTEGER PRIMARY KEY,
    sha TEXT NOT NULL UNIQUE,
    ran_at TEXT NOT NULL,
    exchanges INTEGER DEFAULT 0,        -- chat rows that went in
    fact_ids TEXT DEFAULT '',           -- comma separated, exactly what it created
    episode_id INTEGER,
    episode TEXT DEFAULT '',
    mirror_before TEXT DEFAULT '',      -- MEMORY.md as it stood, for a real diff
    restored_at TEXT                    -- set when this run has been walked back
);
"""

PROMPT_FILE = "dream.md"
DEFAULT_PROMPT = """\
You distil a personal assistant's recent conversation into long-term memory.

Extract durable facts worth remembering in a month (skip chit-chat), and one
sentence summarising what happened.

Reply with ONLY this JSON:
{{"facts": [{{"subject": "<who/what>", "content": "<one sentence>"}}], "episode": "<one sentence>"}}

Exchanges:
{log}"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(LEDGER)


def load_prompt(home: Path) -> str:
    """Written once, read forever. Editing what counts as worth remembering
    should not require editing this repo."""
    path = home / PROMPT_FILE
    if not path.exists():
        path.write_text(DEFAULT_PROMPT, encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    return text if "{log}" in text else DEFAULT_PROMPT


def record(conn: sqlite3.Connection, home: Path, *, exchanges: int, fact_ids: list[int],
           episode_id: int | None, episode: str, mirror_before: str) -> str:
    """One row per run, and the sha that names it. The sha is over what the run
    produced, so two runs that decided the same thing are still two rows — a
    history you can walk needs one entry per event, not per outcome."""
    ensure(conn)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    seed = f"{stamp}|{fact_ids}|{episode}"
    sha = hashlib.sha1(seed.encode()).hexdigest()[:8]
    conn.execute(
        "INSERT INTO dream_runs (sha, ran_at, exchanges, fact_ids, episode_id, episode, "
        "mirror_before) VALUES (?,?,?,?,?,?,?)",
        (sha, stamp, exchanges, ",".join(str(i) for i in fact_ids), episode_id, episode,
         mirror_before))
    conn.commit()
    return sha


def runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    ensure(conn)
    return conn.execute("SELECT * FROM dream_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def find(conn: sqlite3.Connection, sha: str) -> sqlite3.Row | None:
    ensure(conn)
    return conn.execute("SELECT * FROM dream_runs WHERE sha = ?", (sha.strip(),)).fetchone()


def _ids(row: sqlite3.Row) -> list[int]:
    return [int(i) for i in (row["fact_ids"] or "").split(",") if i.strip()]


def render(conn: sqlite3.Connection, limit: int = 10) -> str:
    rows = runs(conn, limit)
    if not rows:
        return "  no dream has run yet — POCKET_CONSOLIDATE_EVERY exchanges trigger one"
    lines = []
    for row in rows:
        mark = " (restored)" if row["restored_at"] else ""
        lines.append(f"  {row['sha']}  {row['ran_at'][:16]}  "
                     f"{len(_ids(row))} facts from {row['exchanges']} rows{mark}")
        if row["episode"]:
            lines.append(f"            episode: {row['episode'][:70]}")
    return "\n".join(lines)


def show(conn: sqlite3.Connection, sha: str) -> str:
    """What one run decided, and what the mirror looked like before it did."""
    row = find(conn, sha)
    if row is None:
        return f"  no dream named '{sha}'. `/dream-log` lists them."
    ids = _ids(row)
    lines = [f"  {row['sha']} · {row['ran_at']} · {row['exchanges']} chat rows in"]
    if row["restored_at"]:
        lines.append(f"  restored at {row['restored_at']} — its facts are retracted")
    if row["episode"]:
        lines.append(f"  episode: {row['episode']}")
    if not ids:
        lines.append("  it added no facts")
    for fact_id in ids:
        fact = conn.execute("SELECT subject, content, forgotten FROM facts WHERE id = ?",
                            (fact_id,)).fetchone()
        if fact is None:
            continue
        state = "retracted" if fact["forgotten"] else "live"
        lines.append(f"  + [{fact_id}] ({state}) {fact['subject']}: {fact['content']}")
    return "\n".join(lines)


def restore(conn: sqlite3.Connection, sha: str) -> str:
    """Retract exactly what one run added. Not a snapshot rollback: a snapshot
    would also undo everything you have told the assistant since."""
    row = find(conn, sha)
    if row is None:
        return f"  no dream named '{sha}'. `/dream-log` lists them."
    if row["restored_at"]:
        return f"  {sha} was already restored at {row['restored_at']}."
    ids = _ids(row)
    if ids:
        conn.execute(f"UPDATE facts SET forgotten = 1 WHERE id IN ({','.join('?' * len(ids))})",
                     ids)
    conn.execute("UPDATE dream_runs SET restored_at = ? WHERE sha = ?",
                 (datetime.now(UTC).isoformat(timespec="seconds"), sha))
    conn.commit()
    return (f"  restored: {len(ids)} fact(s) from {sha} are retracted and will not be "
            f"retrieved again. Every row is still in state.db, marked forgotten.")


def as_json(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """For the dashboard, which shows the same ledger the terminal prints."""
    return [{**dict(row), "facts": len(_ids(row)),
             "mirror_before": (row["mirror_before"] or "")[:2000]}
            for row in runs(conn, limit)]
