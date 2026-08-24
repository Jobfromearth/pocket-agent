"""Tools — a name the model reads, a JSON schema, and a Python function.

Two rules keep the surface honest:
  * a tool NEVER raises into the loop; an error comes back as text the model
    can read and retry from (execute-safely);
  * a tool's return string says exactly what it did and where the artifact
    landed, so the model can only ever relay the truth.

Every registered tool ships in every prompt, so this file stays small on
purpose: three tools, one flagship task.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pocket.hooks import Hooks
from pocket.permissions import Policy


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., str]
    # "safe" runs unattended; "ask" needs a human unless pre-trusted. Anything
    # that reaches outside this process (MCP servers, sub-agents, shells) is "ask".
    risk: str = "safe"
    origin: str = "core"          # core | mcp:<server> | subagent — shown in the UI

    def to_api(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


class ToolRegistry:
    def __init__(self, policy: Policy | None = None, on_result=None,
                 hooks: Hooks | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.policy = policy or Policy()
        # hooks may veto a call or rewrite its result; observers only watch
        self.hooks = hooks or Hooks()
        # hook the loop's observation path: context offloading plugs in here, so
        # neither the loop nor any tool has to know that large results are moved
        self.on_result = on_result or (lambda name, output: output)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.to_api() for t in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def subset(self, names: list[str]) -> ToolRegistry:
        """A scoped registry for a sub-agent: same policy, fewer tools."""
        scoped = ToolRegistry(policy=self.policy, on_result=self.on_result, hooks=self.hooks)
        for name in names:
            tool = self._tools.get(name)
            if tool is not None:
                scoped.register(tool)
        return scoped

    def execute(self, name: str, args: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'"
        # permission first: the deny list has to win over everything, including a
        # call that would have been rejected for a duller reason anyway
        decision = self.policy.check(name, args, tool.risk)
        if not decision.allowed:
            # refused, not raised: the model reads this and can choose another way
            return f"Blocked by policy: {decision.reason}."
        missing = missing_arguments(tool, args)
        if missing:
            # the model can fix this and retry; a TypeError from **args cannot be
            return f"Error calling {name}: missing required argument(s) {', '.join(missing)}"
        vetoed = self.hooks.run("before_tool", name, args)
        if vetoed is not None:
            return vetoed
        try:
            output = self.on_result(name, tool.fn(**args))
        except Exception as exc:            # surface, never crash the loop
            return f"Error running {name}: {exc}"
        return self.hooks.run("after_tool", name, args, output) or output


def missing_arguments(tool: Tool, args: dict[str, Any]) -> list[str]:
    """The registry checks the schema it already published rather than letting
    `fn(**args)` raise: a model that forgot an argument should be told which one,
    in the same shape every other tool error arrives in."""
    required = tool.input_schema.get("required", []) if tool.input_schema else []
    return [key for key in required if key not in args]


def _write_ics(home: Path, title: str, start: str, end: str) -> None:
    """Append a VEVENT so the calendar is a real file you can import."""
    path = home / "calendar.ics"

    def stamp(iso: str) -> str:
        return iso.replace("-", "").replace(":", "") + ("00" if len(iso) == 16 else "")

    body = (path.read_text(encoding="utf-8").replace("END:VCALENDAR\n", "") if path.exists()
            else "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//pocket//EN\n")
    path.write_text(
        f"{body}BEGIN:VEVENT\nSUMMARY:{title}\nDTSTART:{stamp(start)}\nDTEND:{stamp(end)}\n"
        f"END:VEVENT\nEND:VCALENDAR\n", encoding="utf-8")


def build_registry(conn: sqlite3.Connection, home: Path, policy: Policy | None = None,
                   on_result=None, hooks: Hooks | None = None) -> ToolRegistry:
    def create_event(title: str, start: str, end: str = "", attendees: str = "") -> str:
        start_dt = datetime.fromisoformat(start)
        end = end or (start_dt + timedelta(hours=1)).isoformat(timespec="minutes")
        conn.execute("INSERT INTO calendar_events (title, start, \"end\", attendees) "
                     "VALUES (?,?,?,?)", (title, start, end, attendees))
        conn.commit()
        _write_ics(home, title, start, end)
        return (f"Created '{title}' at {start} in the local calendar "
                f"({home / 'state.db'} and {home / 'calendar.ics'}).")

    def list_events(day: str = "") -> str:
        if day in ("", "today"):
            day = datetime.now().date().isoformat()
        rows = conn.execute("SELECT title, start FROM calendar_events "
                            "WHERE start LIKE ? ORDER BY start", (f"{day}%",)).fetchall()
        if not rows:
            return f"No events on {day}."
        return f"Events on {day}: " + "; ".join(f"{r['title']} at {r['start'][11:]}" for r in rows)

    def save_note(subject: str, content: str) -> str:
        conn.execute("INSERT INTO facts (subject, content, source) VALUES (?,?,'user')",
                     (subject.lower().strip(), content))
        conn.commit()
        return f"Saved to memory under '{subject}': {content}"

    registry = ToolRegistry(policy=policy, on_result=on_result, hooks=hooks)
    registry.register(Tool(
        name="create_event",
        description=("Create a calendar event. Resolve relative dates yourself — the current "
                     "date and time are in your system prompt."),
        input_schema={"type": "object", "properties": {
            "title": {"type": "string"},
            "start": {"type": "string", "description": "ISO 8601, e.g. 2026-08-20T09:00"},
            "end": {"type": "string", "description": "ISO 8601; defaults to start + 1 hour"},
            "attendees": {"type": "string", "description": "comma-separated names"}},
            "required": ["title", "start"]},
        fn=create_event))
    registry.register(Tool(
        name="list_events",
        description="Read the calendar for one day. Use it before claiming what is scheduled.",
        input_schema={"type": "object", "properties": {
            "day": {"type": "string", "description": "ISO date, or 'today'"}}, "required": []},
        fn=list_events))
    registry.register(Tool(
        name="save_note",
        description=("Save a durable fact to long-term memory — a preference, a person, a "
                     "project. Use it when the user says 'remember' or shares something lasting."),
        input_schema={"type": "object", "properties": {
            "subject": {"type": "string", "description": "who or what this is about"},
            "content": {"type": "string", "description": "the fact, one sentence"}},
            "required": ["subject", "content"]},
        fn=save_note))
    return registry
