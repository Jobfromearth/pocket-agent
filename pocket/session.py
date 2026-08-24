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
- Never repeat a tool call with the same arguments. Past turns show
  [tools used: ...]; if that exact call already ran, answer from its result.
  Different arguments are a different call — a request with several steps is
  meant to make several calls.
- Relay tool output honestly. Each tool says exactly where its artifact landed;
  never claim anything synced somewhere the tool did not say.

Fanning out. Doing the work yourself is the default; the two tools below cost a
whole extra loop each, so reach for them only when the shape of the request asks
for it:
- One step, or a few steps you can do in sequence yourself -> just do it.
- ONE self-contained sub-task whose intermediate output you do not need to see
  (a long search, a big page to digest) -> `delegate`, naming the smallest tool
  list that can finish it. Only its result comes back, which is the point.
- Anything that has to READ OR WRITE FILES, edit code, or run a command ->
  `delegate_task`. You have no filesystem and no shell; that tool is the only
  way to reach one. Do not use it for work your own tools can already do.
- TWO OR MORE sub-tasks that are independent of each other, or that have a clear
  order -> `assign_team` with a plan: give each task a `key`, its `tools`, and
  `needs` for the ones it must wait on. Tasks without `needs` run in parallel.
  If you do not have `assign_team`, do the steps yourself in order instead.
- Never hand a sub-agent or a worker a task you could finish in one tool call.
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

    def build_system_parts(self, user_message: str, notify=None) -> list[str]:
        """[stable, per-turn]. The split is not cosmetic: a provider's prompt
        cache matches a PREFIX, so anything that changes every turn has to come
        after everything that does not. The clock alone would otherwise
        invalidate the whole system prompt once a minute.

        Stable across a session: who the assistant is, which model it is on, and
        the skill catalog. Per-turn: the time, and whatever the retrieval gate
        decided to pull in."""
        now = datetime.now().astimezone()
        stable = [load_soul(self.settings),
                  (f"You are running on '{self.settings.model}' via the "
                   f"'{self.settings.provider}' provider.")]
        if self.memory is not None:
            stable.append(self.memory.skills.catalog())
        volatile = [f"Right now it is {now:%A, %Y-%m-%d %H:%M} ({now:%Z})."]
        if self.memory is not None:
            retrieved = self.memory.gated_retrieve(user_message, notify=notify)
            if retrieved:
                volatile.append("\nRelevant memory:\n" + retrieved)
        return ["\n".join(p for p in stable if p), "\n".join(p for p in volatile if p)]

    def build_system(self, user_message: str, notify=None) -> str:
        """The same prompt as one string, for every caller that does not care
        where the cache breakpoint goes."""
        return "\n".join(p for p in self.build_system_parts(user_message, notify) if p)

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
