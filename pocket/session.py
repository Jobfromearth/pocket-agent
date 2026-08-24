"""Working memory — rebuilt for every turn, thrown away after it.

    system prompt (SOUL.md)     who the assistant is
  + retrieved memory (gated)    what it remembers, only when the turn needs it
  + the skill catalog           what jobs it knows a procedure for (names only)
  + a windowed chat history     what we just said
  + any matched skill BODY      as its own message, not folded into the prompt
  + the new message

Everything here is ephemeral. What persists lives in memory.py.
"""

from __future__ import annotations

from datetime import datetime

from pocket.config import Settings
from pocket.skills import as_message

DEFAULT_SOUL = """\
You are pocket, a personal assistant running locally on your user's laptop.
You are concise, warm, and you remember what your user tells you.

Rules:
- To schedule anything, call create_event. Resolve relative dates ("next Tuesday",
  "in 30 minutes") yourself from the current time given below; never ask the user
  what time it is.
- To answer what is scheduled, call list_events. You can read the calendar, not
  just write to it.
- When the user shares something durable about a person, project or preference,
  call save_note.
- Call each tool at most once per request. Past turns show [tools used: ...];
  if a tool already ran, answer from that record instead of running it again.
- Relay tool output honestly. Each tool says exactly where its artifact landed;
  never claim anything synced somewhere the tool did not say.
"""


def load_soul(settings: Settings) -> str:
    """SOUL.md is created on first run and is yours to edit. Changing it changes
    who your assistant is — procedural memory at its simplest."""
    path = settings.home / "SOUL.md"
    if not path.exists():
        path.write_text(DEFAULT_SOUL, encoding="utf-8")
    return path.read_text(encoding="utf-8")


class Session:
    """One conversation: the history, and the recipe for the system prompt."""

    def __init__(self, settings: Settings, memory=None):
        self.settings = settings
        self.memory = memory
        self.history: list[dict] = []

    def build_system(self, user_message: str, notify=None) -> str:
        now = datetime.now().astimezone()
        parts = [load_soul(self.settings),
                 f"\nRight now it is {now:%A, %Y-%m-%d %H:%M} ({now:%Z}).",
                 (f"You are running on '{self.settings.model}' via the "
                  f"'{self.settings.provider}' provider.")]
        if self.memory is not None:
            retrieved = self.memory.gated_retrieve(user_message, notify=notify)
            if retrieved:
                parts.append("\nRelevant memory:\n" + retrieved)
            parts.append(self.memory.skills.catalog())
        return "\n".join(part for part in parts if part)

    def messages_for(self, user_message: str, notify=None) -> list[dict]:
        """A bounded window: only the last N turns enter the prompt, so context,
        cost and latency stay flat however long the conversation runs. Older
        turns are not lost — they are in state.db, distilled by consolidation and
        pulled back by the retrieval gate when they matter.

        A skill the matcher is confident about rides in here as its OWN message,
        which is what keeps a job's instructions attributable in the trace and
        droppable by compaction. When the matcher is wrong the model still has
        `read_skill`, and the catalog in the system prompt tells it what to ask
        for."""
        window = self.settings.history_turns * 2
        messages = list(self.history[-window:])
        for skill in (self.memory.skills.match(user_message) if self.memory else []):
            if notify:
                notify("skill", {"name": skill.name, "how": "matched", "chars": len(skill.body)})
            messages.append(as_message(skill))
        messages.append({"role": "user", "content": user_message})
        return messages

    def add_exchange(self, user_message: str, reply: str, tool_calls=None,
                     meta: dict | None = None) -> None:
        """Fold tool activity into the assistant's history entry. Without this the
        model forgets it already acted and cheerfully books the same meeting twice."""
        record = reply
        if tool_calls:
            summary = "; ".join(f"{c['tool']}({c['args']}) -> {c['output']}" for c in tool_calls)
            record = f"{reply}\n[tools used: {summary}]"
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": record})
        if self.memory is not None:
            self.memory.log_chat(user_message, record, meta=meta)
