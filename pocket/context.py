"""Context engineering — the window is the budget, so spend it deliberately.

Four mechanisms, ordered the way they should be reached for: cheapest and most
recoverable first, a model call only when nothing else is left.

  offloading   a 40KB tool result does not belong in the prompt. Write it to
               .pocket/artifacts/, put a short preview and a pointer in context,
               and give the model a `read_artifact` tool for the part it
               actually needs. The full output is never lost — it is on disk.

  compaction   when the conversation outgrows its budget, the oldest turns are
               replaced by ONE summarised turn, and the recent turns are kept
               verbatim. Recency is what a reply needs; the rest is gist.

  fitting      inside one turn the loop keeps appending, so the budget is
               checked before EVERY model call, not once when the turn starts.
               Old tool results are shortened in place — the message stays, so
               a `tool_use` can never lose its `tool_result` and become an
               orphan. No model call, no summary, nothing removed.

  reacting     a provider can still say the prompt is too long. Then, once, the
               oldest results are shortened hard and the call is retried. A
               second refusal raises: retrying forever hides the bug.

Compaction is the only one of the four that costs a model call and the only one
that loses detail, which is why it is last. Every one of the others leaves a
path back to what it shortened — `read_artifact` for an offloaded result,
`read_history` for a compacted conversation. "Nothing is deleted" has to be true
for the MODEL, not just for a human with `sqlite3`.

Compaction is also cache-friendly on purpose: the compacted prefix is stable
across turns, so a provider's prompt cache keeps hitting instead of being
invalidated every time something is dropped.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pocket.tools import Tool

PREVIEW_CHARS = 600
# How many of the most recent tool results stay whole when a turn outgrows its
# budget. Recency is what the next step needs; older results have already been
# read once and are recoverable from the artifact they point at.
KEEP_WHOLE_RESULTS = 3
SHORTENED_TO = 160


def artifacts_dir(home: Path) -> Path:
    path = home / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def offload_if_large(name: str, output: str, home: Path, limit: int = 2000) -> str:
    """Return what the model should see. Small results pass through untouched —
    the machinery only wakes up when it would actually pay for itself."""
    if len(output) <= limit:
        return output
    directory = artifacts_dir(home)
    safe = re.sub(r"[^a-z0-9_]+", "-", name.lower())
    # a monotonic name, never a count of what is in the folder: deleting one
    # artifact would drop the count and the next write would land on a live file
    path = directory / f"{safe}-{datetime.now():%H%M%S}-{uuid4().hex[:6]}.txt"
    path.write_text(output, encoding="utf-8")
    return (f"{output[:PREVIEW_CHARS]}\n\n[... {len(output) - PREVIEW_CHARS} more characters. "
            f"Full output saved to {path.name} ({len(output)} chars). "
            f"Read any part of it with read_artifact(name=\"{path.name}\", start=…, length=…).]")


def make_read_artifact_tool(home: Path) -> Tool:
    def read_artifact(name: str, start: int = 0, length: int = 2000) -> str:
        path = artifacts_dir(home) / Path(name).name      # no traversal out of the folder
        if not path.is_file():
            return f"Error: no artifact named '{name}'"
        text = path.read_text(encoding="utf-8")[start:start + length]
        return text or "(nothing at that offset)"

    return Tool(
        name="read_artifact",
        description=("Read part of a large tool output that was saved to disk instead of "
                     "being pasted into the conversation. Use the name from the pointer."),
        input_schema={"type": "object", "properties": {
            "name": {"type": "string"},
            "start": {"type": "integer", "description": "character offset, default 0"},
            "length": {"type": "integer", "description": "characters to read, default 2000"}},
            "required": ["name"]},
        fn=read_artifact)


def _blocks(message: dict) -> list[dict]:
    """Only the blocks this repo built. An assistant message carries the
    provider SDK's own objects, which are not ours to reach into and are not
    what grows a turn anyway — the tool results are."""
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def fit_for_model(messages: list[dict], budget_chars: int,
                  keep_whole: int = KEEP_WHOLE_RESULTS) -> tuple[list[dict], int]:
    """Shrink OLD tool results until the turn fits. Returns (messages, shrunk).

    This is the step that runs inside the loop, so it obeys two rules the
    cross-turn compaction does not have to care about:

      nothing is removed   a `tool_use` block and its `tool_result` are a pair,
                           and dropping either half produces a request the
                           provider rejects. Shortening a result in place keeps
                           every pair intact by construction
      no model call        summarising inside the loop would spend a call per
                           iteration to save a call per iteration

    What is left behind is the head of the result, which for an offloaded one
    still carries its `read_artifact` pointer — so the model can pull the detail
    back if it turns out to matter.
    """
    def size() -> int:
        return sum(len(str(m.get("content", ""))) for m in messages)

    if size() <= budget_chars:
        return messages, 0
    results = [(index, block) for index, message in enumerate(messages)
               for block in _blocks(message) if block.get("type") == "tool_result"]
    shrunk = 0
    for _index, block in results[:max(0, len(results) - keep_whole)]:
        text = str(block.get("content", ""))
        if len(text) <= SHORTENED_TO:
            continue
        block["content"] = (f"{text[:SHORTENED_TO]}… [shortened to fit the window; "
                            f"{len(text)} chars originally]")
        shrunk += 1
        if size() <= budget_chars:
            break
    return messages, shrunk


def make_read_history_tool(conn: sqlite3.Connection) -> Tool:
    """The way back from a compacted conversation.

    `chat_log` has always held every message, but until this tool existed only a
    human with `sqlite3` could reach it — which made "nothing is deleted" true
    for the wrong reader. The compacted message names this tool, so the model
    can go and look instead of guessing at what it used to know."""
    def read_history(offset: int = 0, limit: int = 10) -> str:
        rows = conn.execute(
            "SELECT id, role, content FROM chat_log ORDER BY id DESC LIMIT ? OFFSET ?",
            (max(1, min(limit, 40)), max(0, offset))).fetchall()
        if not rows:
            return f"No messages at offset {offset}."
        lines = [f"[#{row['id']}] {row['role']}: {str(row['content'])[:400]}"
                 for row in reversed(rows)]
        return "\n".join(lines)

    return Tool(
        name="read_history",
        description=("Read earlier messages of this conversation from the record, newest "
                     "first. Use it when a compacted summary is missing a detail you need. "
                     "`offset` skips that many of the most recent messages."),
        input_schema={"type": "object", "properties": {
            "offset": {"type": "integer", "description": "how many recent messages to skip"},
            "limit": {"type": "integer", "description": "how many to read, 1-40 (default 10)"}},
            "required": []},
        fn=read_history)


COMPACTION_PROMPT = """\
Summarise this earlier part of a conversation between a user and their assistant.
Keep decisions, commitments, names, dates and anything the assistant must not
forget. Drop pleasantries. Write at most 8 short lines, no preamble.

{log}"""


def compact_history(history: list[dict], budget_chars: int,
                    summarise: Callable[[str], str], keep_turns: int = 2) -> tuple[list[dict], int]:
    """Returns (history, how many messages were folded into the summary).

    Nothing is deleted from the record — `chat_log` in state.db still holds every
    message. This only decides what enters the next prompt."""
    if sum(len(str(m["content"])) for m in history) <= budget_chars:
        return history, 0
    keep = keep_turns * 2
    old, recent = history[:-keep], history[-keep:]
    if not old:
        return history, 0
    log = "\n".join(f"{m['role']}: {m['content']}" for m in old)
    try:
        summary = summarise(COMPACTION_PROMPT.format(log=log))
    except Exception:
        return history, 0             # a failed summariser must not drop context
    if not summary.strip():
        return history, 0
    folded = [{"role": "user", "content":
               f"[earlier conversation, compacted — {len(old)} messages. The full text is still "
               f"on the record: call read_history(offset, limit) if you need a detail this "
               f"summary lost.]\n{summary.strip()}"},
              {"role": "assistant", "content": "Noted — I have the earlier context."}]
    return folded + recent, len(old)
