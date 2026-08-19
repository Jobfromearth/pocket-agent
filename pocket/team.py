"""Teams — several workers, one board. Delegation past the point of one task.

`delegate` hands ONE sub-task to ONE sub-agent (subagent.py). The moment a
request needs three steps and two of them are independent, the hard questions
stop being about the sub-agent and start being about coordination: who runs
first, what does a worker get to see, and what happens when one of them fails?

ClawTeam answers those with a filesystem — a JSON board, kanban states,
`--blocked-by` dependencies, point-to-point inboxes. The idea worth stealing is
not the JSON: it is that **the plan is data, not a conversation**. Workers never
negotiate in free text, so a team is something you can read after the fact.

Four mechanisms, all of them already in this repo, wired together:

    the board     one `tasks` row per task in state.db, so `sqlite3` shows the
                  kanban: pending -> running -> done, or failed / blocked
    the schedule  the task DAG becomes a Graph and graph.py's wave scheduler
                  runs independent tasks in parallel. A dependency finishing IS
                  the unblock — there is no separate unblock step to get wrong
    the channel   a worker is handed its dependencies' RESULTS as context and
                  nothing else. That is the whole inbox, and it only flows one
                  way: down the DAG. No worker can see, or write to, a sibling
    the radius    a worker is one run_loop with a scoped registry, and it may
                  never start a team or delegate — one level, by construction

Failure is the part most swarms get wrong. When a worker fails, its dependents
are marked `blocked` and never run: a board that says "this did not happen"
is worth more than one that quietly ran the next step on missing input.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from pocket.graph import END, START, Graph, Node, run_graph
from pocket.loop import run_loop
from pocket.tools import Tool, ToolRegistry

# A plan the model writes must stay small enough to read in one screen, and a
# runaway plan must cost nothing: both caps are refusals, not truncations.
MAX_TASKS = 8
# Never handed to a worker, whatever the plan asks for: one level of delegation.
RESERVED = ("assign_team", "delegate")
RESULT_PREVIEW = 300

WORKER_SYSTEM = """\
You are one worker on a team and you own EXACTLY one task. Do it with the tools
you were given, then reply with the result in one or two sentences — no preamble,
no questions, no offering to do more. If your tools cannot do it, say exactly why.

Team goal: {goal}
Your task: {task}
{inbox}"""

INBOX = """
Results you depend on — this is everything you get to see from the other workers:
{results}"""


@dataclass
class Task:
    key: str
    instruction: str
    tools: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


def _names(value) -> list[str]:
    """Tool lists and dependency lists arrive as a JSON array from one model and
    a comma-separated string from the next. Both are the same thing."""
    if value is None:
        return []
    if isinstance(value, str):
        return [name.strip() for name in value.split(",") if name.strip()]
    return [str(name).strip() for name in value if str(name).strip()]


def parse_plan(plan) -> list[Task]:
    """Validate hard, before a single worker spends a token. Every failure here
    is a ValueError the caller turns into text the model can read and retry."""
    if isinstance(plan, str):
        plan = json.loads(plan)
    if not isinstance(plan, list) or not plan:
        raise ValueError("plan must be a non-empty JSON list of tasks")
    if len(plan) > MAX_TASKS:
        raise ValueError(f"plan has {len(plan)} tasks; the cap is {MAX_TASKS}")
    tasks: list[Task] = []
    for entry in plan:
        if not isinstance(entry, dict):
            raise ValueError(f"task must be an object, got {type(entry).__name__}")
        key = str(entry.get("key") or f"task{len(tasks) + 1}").strip()
        instruction = str(entry.get("task") or entry.get("instruction") or "").strip()
        if not instruction:
            raise ValueError(f"task '{key}' has no instruction")
        tasks.append(Task(key, instruction, _names(entry.get("tools")),
                          _names(entry.get("needs") or entry.get("depends_on"))))
    keys = [task.key for task in tasks]
    if len(set(keys)) != len(keys):
        raise ValueError("task keys must be unique")
    for task in tasks:
        for need in task.depends_on:
            if need not in keys:
                raise ValueError(f"task '{task.key}' depends on unknown task '{need}'")
            if need == task.key:
                raise ValueError(f"task '{task.key}' depends on itself")
    _check_acyclic(tasks)
    return tasks


def _check_acyclic(tasks: list[Task]) -> None:
    """Kahn's algorithm, eight lines. A cycle is a plan that can never start,
    and the honest moment to say so is now — not after a deadlock."""
    waiting = {task.key: set(task.depends_on) for task in tasks}
    settled: set[str] = set()
    while waiting:
        ready = [key for key, needs in waiting.items() if needs <= settled]
        if not ready:
            raise ValueError(f"plan has a dependency cycle: {', '.join(sorted(waiting))}")
        settled.update(ready)
        for key in ready:
            del waiting[key]


def worker_tools(registry: ToolRegistry, wanted: list[str]) -> ToolRegistry:
    """The scoped registry a worker gets: what the plan asked for (or everything,
    if it asked for nothing) minus the names that would let it fan out again."""
    allowed = [name for name in (wanted or registry.names()) if name not in RESERVED]
    return registry.subset(allowed)


class Board:
    """The kanban, in state.db. Nothing here is in memory only — a run you can
    inspect afterwards with `sqlite3 .pocket/state.db 'select * from tasks'` is
    the entire reason the board is a table and not a dict."""

    def __init__(self, conn: sqlite3.Connection, team: str):
        self.conn = conn
        self.team = team

    def post(self, tasks: list[Task]) -> None:
        self.conn.executemany(
            "INSERT INTO tasks (team, key, instruction, tools, depends_on) VALUES (?,?,?,?,?)",
            [(self.team, t.key, t.instruction, ",".join(t.tools), ",".join(t.depends_on))
             for t in tasks])
        self.conn.commit()

    def mark(self, key: str, status: str, result: str = "") -> None:
        self.conn.execute(
            "UPDATE tasks SET status=?, result=?, updated_at=datetime('now') "
            "WHERE team=? AND key=?", (status, result, self.team, key))
        self.conn.commit()

    def settle(self) -> None:
        """Anything still pending after the run never became ready — say that on
        the board rather than leaving a row that looks like it is still coming."""
        self.conn.execute(
            "UPDATE tasks SET status='blocked', result=?, updated_at=datetime('now') "
            "WHERE team=? AND status IN ('pending','running')",
            ("not run — a task it depends on did not finish", self.team))
        self.conn.commit()

    def rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tasks WHERE team=? ORDER BY id",
                                 (self.team,)).fetchall()

    def render(self, width: int = RESULT_PREVIEW) -> str:
        """One line per task: the same view the model reads and a human reads,
        because two renderings of one board is two chances to disagree."""
        rows = self.rows()
        tally = {}
        for row in rows:
            tally[row["status"]] = tally.get(row["status"], 0) + 1
        head = (f"{self.team} · {len(rows)} tasks: "
                + ", ".join(f"{count} {status}" for status, count in sorted(tally.items())))
        lines = [head]
        for row in rows:
            result = (row["result"] or "").replace("\n", " ")
            if len(result) > width:
                result = result[:width] + "…"
            needs = f" (needs {row['depends_on']})" if row["depends_on"] else ""
            lines.append(f"  [{row['key']}] {row['status']}{needs}: {result or '—'}")
        return "\n".join(lines)


def run_team(client, model: str, registry: ToolRegistry, conn: sqlite3.Connection, *,
             goal: str, plan, team: str = "", max_iterations: int = 4,
             max_tokens: int = 2048, observer=None) -> Board:
    """Run one plan to completion and return its board. Raises only for a plan
    that cannot be run at all; a worker that fails is a row, not an exception."""
    tasks = parse_plan(plan)
    board = Board(conn, team or f"team-{datetime.now():%Y%m%d-%H%M%S}")
    board.post(tasks)

    def worker(task: Task):
        def work(state: dict) -> dict:
            notify = state.get("_notify") or (lambda kind, event: None)
            board.mark(task.key, "running")
            inbox = "\n".join(f"[{need}] {state.get(f'result:{need}', '(missing)')}"
                              for need in task.depends_on)
            system = WORKER_SYSTEM.format(
                goal=state.get("goal", ""), task=task.instruction,
                inbox=INBOX.format(results=inbox) if inbox else "")
            try:
                result = run_loop(
                    client=client, model=model, system=system,
                    messages=[{"role": "user", "content": task.instruction}],
                    tools=worker_tools(registry, task.tools), max_iterations=max_iterations,
                    max_tokens=max_tokens, observer=notify)
            except Exception as exc:
                # the row records the failure before the graph engine sees it, so
                # the board is already truthful by the time dependents are blocked
                board.mark(task.key, "failed", f"{type(exc).__name__}: {exc}")
                raise
            board.mark(task.key, "done", result.reply)
            return {f"result:{task.key}": result.reply}

        return work

    graph = Graph(f"team:{board.team}")
    for task in tasks:
        graph.add_node(Node(task.key, worker(task), kind="agent"))
    blockers = {need for task in tasks for need in task.depends_on}
    for task in tasks:
        for need in task.depends_on or [START]:
            graph.add_edge(need, task.key)
        if task.key not in blockers:
            graph.add_edge(task.key, END)
    run_graph(graph, {"goal": goal}, observer=observer, max_steps=len(tasks) + 2)
    board.settle()
    return board


PLAN_HELP = """\
JSON list of tasks, e.g. [{"key":"draft","task":"…","tools":"save_note"},
{"key":"book","task":"…","tools":"create_event","needs":"draft"}]. `needs` names
the tasks that must finish first; tasks with no `needs` run in parallel."""


def make_team_tool(client, model: str, registry: ToolRegistry, conn: sqlite3.Connection, *,
                   max_iterations: int = 4, max_tokens: int = 2048, observer=None) -> Tool:
    def assign_team(goal: str, plan: str) -> str:
        try:
            board = run_team(client, model, registry, conn, goal=goal, plan=plan,
                             max_iterations=max_iterations, max_tokens=max_tokens,
                             observer=observer)
        except (ValueError, json.JSONDecodeError) as exc:
            # a bad plan is a message the model can fix and retry, never a crash
            return f"Error: {exc}"
        return board.render()

    return Tool(
        name="assign_team",
        description=("Run several sub-tasks as a team over a shared board, when a request "
                     "splits into steps that are independent or ordered. Independent tasks "
                     "run in parallel; a task listing `needs` starts only after those finish "
                     "and receives their results. Prefer `delegate` for a single sub-task."),
        input_schema={"type": "object", "properties": {
            "goal": {"type": "string", "description": "one sentence: what the team is for"},
            "plan": {"type": "string", "description": PLAN_HELP}},
            "required": ["goal", "plan"]},
        fn=assign_team,
        risk="ask",                 # several loops, real tools: a human sees it first
        origin="team")
