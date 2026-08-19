# Architecture

One turn, end to end, and where each part of it lives. Everything below is one
file you can open; nothing is generated, injected, or hidden behind a factory.

## A turn

```
you > Book a catch-up with Alex tomorrow
  │
  ├─ __main__.py      is it a /command? then answer locally and stop
  │
  ├─ agent.py         respond(): starts the trace, picks a front door
  │   │
  │   ├─ graph.py     (POCKET_GRAPH_WORKFLOWS=1) triage: classify with the small
  │   │               model WHILE the calendar loads, then route. Any failure
  │   │               here falls through to the plain loop below.
  │   │
  │   └─ _full_turn()
  │       ├─ session.py    build_system(): SOUL.md + time + gated memory + skills
  │       │   └─ memory.py     should_retrieve() -> FTS5 search, or nothing
  │       ├─ context.py    compact_history(): fold the oldest turns if over budget
  │       └─ loop.py       reason -> act -> observe, until the model stops asking
  │            └─ tools.py     execute(): permissions.py decides, then the function
  │                 ├─ context.py   offload_if_large(): >2KB goes to artifacts/
  │                 ├─ mcp.py       mcp__<server>__<tool> over stdio
  │                 ├─ subagent.py  delegate: one more loop, fewer tools
  │                 └─ team.py      assign_team: several loops over one board
  │
  ├─ session.py       add_exchange(): history + chat_log row with per-turn meta
  ├─ memory.py        maybe_consolidate(): every N exchanges, distil facts
  └─ trace.py         turn_end + one usage.jsonl line per model call
```

## The map

| File | One sentence |
|---|---|
| `config.py` | every knob, as one dataclass of env vars |
| `session.py` | assembles working memory; owns SOUL.md |
| `agent.py` | wiring, and the only place a turn is orchestrated |
| `__main__.py` | the terminal gateway: subcommands and `/` commands |
| `loop.py` | the loop, with its two exits |
| `models.py` | providers: Anthropic shape, OpenAI shape, offline stub |
| `tools.py` | the registry: name, schema, function, risk |
| `memory.py` | semantic / episodic / procedural, gate, consolidation |
| `db.py` | the schema, and the one connection everything shares |
| `context.py` | offloading and compaction |
| `mcp.py` | an MCP client for other people's servers |
| `permissions.py` | deny list, ask-the-human, session grants |
| `subagent.py` | one sub-task, one sub-agent, one result |
| `team.py` | several sub-tasks over one board, scheduled by their dependencies |
| `graph.py` | wave scheduler, code routers, and the triage workflow |
| `trace.py` | JSONL trace + spend ledger |
| `evals.py` | the deterministic suite and the release gate |

## Invariants

These are the rules the code keeps, and the eval suite exists to keep them true:

1. **A turn ends in exactly two ways** — the model stops asking for tools, or
   `max_iterations` is hit and the reply says so. (`loop.py`)
2. **Nothing judge-shaped can remove capability.** A broken gate retrieves; a
   broken classifier routes to the full loop; a broken graph falls back to the
   loop; a broken summariser keeps the context it could not fold.
3. **A tool never raises into the loop.** Errors, refusals and blocked plans all
   come back as text the model can read and act on. (`tools.py`)
4. **Third-party capability asks first.** MCP tools, `delegate` and
   `assign_team` are all `risk="ask"`. (`permissions.py`)
5. **Nothing is deleted to save context.** Offloaded results are on disk;
   compacted turns are still rows in `chat_log`. (`context.py`)
6. **Fan-out is one level deep.** A sub-agent cannot delegate; a worker can
   neither delegate nor start a team. (`subagent.py`, `team.py`)
7. **The record is queryable.** Memory, calendar, chat log and the team board
   are tables in one SQLite file you can open with `sqlite3`.

## State on disk

```
.pocket/
  state.db          facts, episodes, chat_log, calendar_events, tasks (+ FTS5)
  SOUL.md           the system prompt, yours to edit
  MEMORY.md         a generated mirror of what is remembered
  calendar.ics      a real calendar file, importable
  mcp.json          which MCP servers to connect
  artifacts/        tool results too large for the prompt
  traces/<date>.jsonl   every event of every turn
  usage.jsonl       one line per model call: tokens and an estimated cost
```

## Where to extend it

| You want | Touch | Because |
|---|---|---|
| another gateway (bot, web, voice) | a new file next to `__main__.py` | gateways only call `respond()` |
| another provider | one row in `PROVIDERS` | `models.py` adapts wire formats |
| another tool | `build_registry()` | one name, one schema, one function |
| capability you did not write | `.pocket/mcp.json` | it arrives gated, namespaced |
| another workflow | `graph.py` | nodes + a code router, fails open |
| a different fan-out policy | `team.py` | the plan is data; the scheduler is the graph |
