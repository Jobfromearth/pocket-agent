"""Memory — three pillars plus the two agents that manage them.

    procedural   SKILL.md files       how to act        (catalogued always, body
                                                     on demand - see skills.py)
    semantic     facts + FTS5         what is true      (durable, searchable)
    episodic     episodes + FTS5      what happened     (dated)

    retrieval gate    decides IF a turn needs memory at all
    consolidation     distils the chat log into facts, every N exchanges

The gate is the part worth stealing. Retrieving on every turn is slow AND
worse: irrelevant memories bias the answer. So a cheap model answers one
narrow question first — does THIS message need the user's memory? — and both
the decision and its reason are traced, so you can audit it later.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from pocket.config import Settings
from pocket.skills import SkillLoader

# ---------------------------------------------------------------- semantic
# FTS5's default tokenizer keeps every Unicode alphanumeric but does not
# segment CJK, so a whole run is indexed as ONE term. Those tokens, and only
# those, are searched as prefixes — turning every token into `token*` would
# quietly make "car" match "carpet", a worse failure because it still looks
# like it works.
_UNSEGMENTED = re.compile("[぀-ヿ㐀-䶿一-鿿]")


def fts_query(text: str) -> str:
    """User text is not a valid FTS5 query (quotes and punctuation break MATCH).
    Reduce it to `word OR word`, lowercased — lowercase is load-bearing, since
    FTS5's AND/OR/NOT are operators only in uppercase."""
    words = re.findall(r"[^\W_]{2,}", text.lower())
    if not words:
        return ""
    return " OR ".join(f"{w}*" if _UNSEGMENTED.search(w) else w for w in dict.fromkeys(words))


class FactStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, subject: str, content: str, source: str = "user") -> None:
        self.conn.execute("INSERT INTO facts (subject, content, source) VALUES (?,?,?)",
                          (subject.lower().strip(), content, source))
        self.conn.commit()

    def search(self, query: str, top_k: int = 4) -> list[str]:
        match = fts_query(query)
        if not match:
            return []
        rows = self.conn.execute(
            "SELECT f.subject, f.content FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?", (match, top_k)).fetchall()
        return [f"[{r['subject']}] {r['content']}" for r in rows]


class EpisodeStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, summary: str, happened_at: str) -> None:
        self.conn.execute("INSERT INTO episodes (happened_at, summary) VALUES (?,?)",
                          (happened_at, summary))
        self.conn.commit()

    def search(self, query: str, top_k: int = 3) -> list[str]:
        """Relevance first (FTS rank), then recency — RAG for relevance, SQL for time."""
        match = fts_query(query)
        if not match:
            return []
        rows = self.conn.execute(
            "SELECT e.happened_at, e.summary FROM episodes_fts JOIN episodes e "
            "ON e.id = episodes_fts.rowid WHERE episodes_fts MATCH ? "
            "ORDER BY rank, e.happened_at DESC LIMIT ?", (match, top_k)).fetchall()
        return [f"({r['happened_at']}) {r['summary']}" for r in rows]


# -------------------------------------------------------------- procedural
# ------------------------------------------------------- the two managers
GATE_PROMPT = """\
You are a retrieval gate for a personal assistant's long-term memory.
Given the user's message, decide if answering well requires stored memories
(facts about people, projects, preferences, or past events).

Reply with ONLY this JSON, nothing else:
{{"retrieve": true/false, "query": "<search keywords if true, else empty>", "reason": "<5 words>"}}

General knowledge, math, or small talk -> false.
Anything referencing the user's life, people, plans, or history -> true.

User message: {message}"""

SUMMARISER_PROMPT = """\
You distil a personal assistant's recent conversation into long-term memory.

Extract durable facts worth remembering in a month (skip chit-chat), and one
sentence summarising what happened.

Reply with ONLY this JSON:
{{"facts": [{{"subject": "<who/what>", "content": "<one sentence>"}}], "episode": "<one sentence>"}}

Exchanges:
{log}"""


def _json_reply(client, model: str, prompt: str, max_tokens: int = 800) -> dict | None:
    """One narrow question to a cheap model, answered as JSON. Reasoning models
    emit a thinking block before the JSON, so slice from the first brace rather
    than trusting the whole reply to parse."""
    response = client.messages.create(model=model, max_tokens=max_tokens,
                                      messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in response.content if b.type == "text")
    if "{" not in text:
        return None
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def should_retrieve(client, small_model: str, message: str) -> tuple[bool, str, str]:
    """(retrieve?, query, reason). Fails OPEN: if the gate breaks we retrieve —
    a stale memory beats a lost one, and a broken judge must cost latency,
    never capability."""
    try:
        decision = _json_reply(client, small_model, GATE_PROMPT.format(message=message))
        if decision is None:
            return True, message, "gate returned no JSON - failing open"
        return (bool(decision.get("retrieve")), decision.get("query") or message,
                decision.get("reason", ""))
    except Exception as exc:
        return True, message, f"gate failed open ({type(exc).__name__})"


class Memory:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, client):
        self.conn = conn
        self.settings = settings
        self.client = client
        self.facts = FactStore(conn)
        self.episodes = EpisodeStore(conn)
        self.skills = SkillLoader([Path(__file__).parent / "skills", settings.home / "skills"])

    # ---- read paths
    def gated_retrieve(self, message: str, notify=None) -> str:
        retrieve, query, reason = should_retrieve(self.client, self.settings.small_model, message)
        if notify:
            notify("gate", {"decision": "retrieve" if retrieve else "skip", "reason": reason})
        if not retrieve:
            return ""
        found = self.facts.search(query, self.settings.retrieval_top_k)
        found += self.episodes.search(query, top_k=3)
        return "\n".join(found)


    # ---- write paths
    def log_chat(self, user_message: str, reply: str, meta: dict | None = None) -> None:
        self.conn.execute("INSERT INTO chat_log (role, content) VALUES ('user', ?)",
                          (user_message,))
        self.conn.execute("INSERT INTO chat_log (role, content, meta) VALUES ('assistant', ?, ?)",
                          (reply, json.dumps(meta) if meta else None))
        self.conn.commit()

    def maybe_consolidate(self, notify=None) -> int:
        """The whiteboard's diamond: only consolidate after N new exchanges.
        Summarising after every message is noisy and wasteful; a batch gives the
        summariser enough context to find facts worth keeping."""
        rows = self.conn.execute(
            "SELECT id, role, content FROM chat_log WHERE consolidated = 0 ORDER BY id").fetchall()
        if len(rows) < self.settings.consolidate_every * 2:     # 2 rows per exchange
            return 0
        log = "\n".join(f"{r['role']}: {r['content']}" for r in rows)
        try:
            distilled = _json_reply(self.client, self.settings.small_model,
                                    SUMMARISER_PROMPT.format(log=log), max_tokens=2048)
        except Exception:
            return 0            # never lose the log: it stays unconsolidated for next time
        if distilled is None:
            return 0
        for fact in distilled.get("facts", []):
            if fact.get("subject") and fact.get("content"):
                self.facts.add(fact["subject"], fact["content"], source="consolidation")
        if distilled.get("episode"):
            self.episodes.add(distilled["episode"], happened_at=date.today().isoformat())
        self.conn.execute(
            f"UPDATE chat_log SET consolidated = 1 WHERE id IN ({','.join('?' * len(rows))})",
            [r["id"] for r in rows])
        self.conn.commit()
        count = len(distilled.get("facts", []))
        if count and notify:
            notify("consolidation", {"new_facts": count})
        return count

    def export_markdown(self) -> None:
        """state.db stays the queryable source of truth; MEMORY.md is a generated
        mirror, so 'your memory is a file you can open' is literally true."""
        facts = self.conn.execute("SELECT subject, content FROM facts ORDER BY subject, id").fetchall()
        eps = self.conn.execute(
            "SELECT happened_at, summary FROM episodes ORDER BY happened_at DESC, id DESC").fetchall()
        lines = ["# pocket memory", "",
                 "_Generated after every turn. Source of truth: `state.db`._", "",
                 f"## Facts ({len(facts)})", ""]
        lines += [f"- **{f['subject']}** - {f['content']}" for f in facts] or ["_none yet_"]
        lines += ["", f"## Episodes ({len(eps)})", ""]
        lines += [f"- **{e['happened_at']}** - {e['summary']}" for e in eps] or ["_none yet_"]
        (self.settings.home / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
