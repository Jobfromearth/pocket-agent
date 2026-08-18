"""Context engineering — the window is the budget, so spend it deliberately.

Two mechanisms, both of which show up the moment an agent does real work:

  offloading   a 40KB tool result does not belong in the prompt. Write it to
               .pocket/artifacts/, put a short preview and a pointer in context,
               and give the model a `read_artifact` tool for the part it
               actually needs. The full output is never lost — it is on disk.

  compaction   when the conversation outgrows its budget, the oldest turns are
               replaced by ONE summarised turn, and the recent turns are kept
               verbatim. Recency is what a reply needs; the rest is gist.

Both are cache-friendly on purpose: the compacted prefix is stable across turns,
so a provider's prompt cache keeps hitting instead of being invalidated every
time something is dropped.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from pocket.tools import Tool

PREVIEW_CHARS = 600


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
    path = directory / f"{safe}-{len(list(directory.glob('*.txt'))) + 1}.txt"
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
    folded = [{"role": "user", "content": f"[earlier conversation, compacted]\n{summary.strip()}"},
              {"role": "assistant", "content": "Noted — I have the earlier context."}]
    return folded + recent, len(old)
