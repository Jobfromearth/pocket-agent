"""Working memory — rebuilt for every turn, thrown away after it.

    system prompt (SOUL.md)     who the assistant is
  + retrieved memory (gated)    what it remembers, only when the turn needs it
  + matching skills             how to do this particular job
  + a windowed chat history     what we just said
  + the new message

Everything here is ephemeral. What persists lives in memory.py.
"""

from __future__ import annotations

from datetime import datetime

from pocket.config import Settings

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
            skills = self.memory.matching_skills(user_message)
            if skills:
                parts.append("\nRelevant skill instructions:\n" + skills)
        return "\n".join(parts)

    def messages_for(self, user_message: str) -> list[dict]:
        """A bounded window: only the last N turns enter the prompt, so context,
        cost and latency stay flat however long the conversation runs. Older
        turns are not lost — they are in state.db, distilled by consolidation and
        pulled back by the retrieval gate when they matter."""
        window = self.settings.history_turns * 2
        return self.history[-window:] + [{"role": "user", "content": user_message}]

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
