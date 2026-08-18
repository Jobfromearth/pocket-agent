# pocket-agent

**Your own assistant. On your laptop. Small enough to read in one evening.**

I wanted an assistant that remembers my life and runs on my own machine — not a product
I rent, and not a framework that hides the interesting parts behind three layers of
abstraction. So I wrote the smallest thing that still has every part a serious agent
needs, and kept every part readable: **one mechanism, one file.**

It runs with **no API key and no dependencies**, so you can watch it work ten seconds
after cloning:

```bash
python -m pocket demo     # a scripted tour: memory, the gate, triage, a real tool call
python -m pocket eval     # 46 deterministic checks + the release gate, in under a second
python -m pocket mcp      # start an MCP server and call a tool through it, no model involved
python -m pocket team     # three workers over one board: two in parallel, one that waits
python -m pocket tools    # what the model can call — and what it may do without asking
python -m pocket          # chat for real (put a key in .env)
```

Inside a chat, `/help` `/tools` `/context` `/memory` `/board` `/new` are answered by the
harness itself and never reach a model.

The core imports **stdlib only** and `pocket/*.py` totals **3,133 lines**; `anthropic` /
`openai` load lazily, and only if you point it at that provider. That line count is not
decoration — [an eval asserts it is still true](pocket/evals.py), and
`./scripts/line_budget.sh` prints it per pillar.

## What's inside

| Pillar | Files | What lives there |
|---|---|---|
| **Harness** | `config.py` `session.py` `agent.py` `__main__.py` | working-memory assembly, wiring, the terminal gateway |
| **Loop** | `loop.py` `models.py` `tools.py` | reason→act→observe with two guardrails; 2 wire formats behind one loop |
| **Memory** | `memory.py` `db.py` | semantic (FTS5) / episodic / procedural, a retrieval gate, consolidation |
| **Context** | `context.py` | large results offloaded to disk; history compacted when it outgrows its budget |
| **Reach** | `mcp.py` `subagent.py` | MCP tools from other people's servers; delegation to a scoped sub-agent |
| **Team** | `team.py` | several workers over one shared board, scheduled by their dependencies |
| **Safety** | `permissions.py` | deny list, ask-the-human, per-session grants — refusals come back as text |
| **Graph** | `graph.py` | structure *around* the loop: parallel nodes, code routers, fail-open |
| **Ops** | `trace.py` `evals.py` | JSONL trace, spend ledger, deterministic evals, release gate |

State lives in `.pocket/`: `state.db` (SQLite + FTS5 — memory, calendar, chat log and the
team board), `calendar.ics`, `MEMORY.md`, `artifacts/`, `traces/<date>.jsonl`,
`usage.jsonl`. All of it yours to open.

## Nine decisions worth defending

1. **The retrieval gate.** Most agents query memory every turn. That is slow, and worse:
   irrelevant memories bias the answer. A cheap model answers one narrow question first —
   *does this message need memory?* — and the decision plus its reason land in the trace,
   so it is auditable rather than magic. `memory.py`

2. **Everything judge-shaped fails open.** A broken gate retrieves anyway; a broken triage
   classifier routes to the full loop; a broken graph node drops the turn back to the plain
   loop; a failed summariser keeps the context it could not compact. A degraded part may
   cost latency — never capability, and never data. `agent.py`, `context.py`

3. **The loop has exactly two exits.** The model stops asking for tools, or `max_iterations`
   is reached and it says so honestly. There is no third way for a turn to end. `loop.py`

4. **MCP, written for the 2026-07-28 revision.** That revision made MCP *stateless*: no
   `initialize` handshake, no session id — every request carries its protocol version and
   client capabilities in `_meta`. On stdio there is no HTTP status code to fall back on, so
   the spec's own advice is to probe with `server/discover` and treat failure as "this one is
   legacy". Both paths are implemented, and both are covered by evals against a bundled
   server that can speak either dialect. `mcp.py`, `examples/demo_server.py`

5. **Third-party capability is gated by default.** Every MCP tool and the sub-agent are
   `risk="ask"`: a human sees them before they run, once per session. A short deny list wins
   over any confirmation. A refusal is returned to the model as text, like a tool error, so
   the turn continues down another path instead of the process dying. `permissions.py`

6. **The context window is a budget, spent deliberately.** A 40KB tool result is written to
   `artifacts/` and replaced by a preview plus a pointer, with a `read_artifact` tool for the
   part that matters; when the conversation outgrows its budget the oldest turns fold into one
   summary and recent turns stay verbatim. Nothing is deleted — `state.db` still has every
   message. `context.py`

7. **Delegation without handing over control.** A sub-agent is one more `run_loop` with a
   narrower brief and a smaller registry. It runs inside a single tool call, only its *result*
   crosses back, and it cannot delegate again. The blast radius is exactly the tool list you
   passed. `subagent.py`

8. **A team is a board, not a swarm.** When several sub-tasks are independent, the
   interesting question stops being "what is a sub-agent" and becomes coordination. So a
   plan is *data* — keys, tool allow-lists, `needs` — never a conversation between agents.
   The DAG is executed by the graph engine that was already here, so independent tasks run
   in one wave; a worker receives the results of exactly what it declared it needs; and a
   failed worker leaves its dependents `blocked` rather than running them on missing input.
   The board is a table in `state.db`, so a run is readable after the fact. `team.py`

9. **Deterministic evals and judged evals never share a file.** "Did `create_event` fire with
   the right arguments and did the row land?" is a unit test. "Was the reply good?" is a scored
   judgement. Collapsing the two is the most common eval mistake; here the deterministic suite
   gates the release. `evals.py`

## The graph, briefly

A loop *discovers* what to do next; a graph *pre-determines* it. Both ship. The engine runs
nodes in waves — parallel nodes must write disjoint keys or it raises — and routers are plain
Python over the shared state, so a model never decides control flow directly. The shipped
workflow is triage: classify the message with a small model *while* the calendar loads, then
route. `thanks!` never wakes the big model; a real task runs the same `_full_turn` the
flag-off path runs, so loop-as-a-node cannot drift from loop-as-default.

```bash
POCKET_GRAPH_WORKFLOWS=1 python -m pocket
```

## The team board, briefly

`delegate` hands one sub-task to one sub-agent. `assign_team` takes a plan — a JSON list of
tasks with `key`, `tools` and `needs` — writes each task as a row in `state.db`, and lets
the wave scheduler run it: independent tasks go together, a dependency finishing *is* the
unblock, and each worker's system prompt carries only the results of the tasks it named.
Nothing else crosses between workers; there is no peer chat and no shared scratchpad.

```bash
python -m pocket team                      # a fixed plan, offline, no model deciding
POCKET_TEAM=1 python -m pocket             # let the model call assign_team (it asks first)
sqlite3 .pocket/state.db "select key, status from tasks"    # the kanban, afterwards
```

An invalid plan (a cycle, an unknown dependency, more than 8 tasks) is refused before a
single worker spends a token, and the refusal goes back to the model as text so it can fix
it. Full detail, including what it deliberately does not do: [docs/teams.md](docs/teams.md).

## Connecting an MCP server

`.pocket/mcp.json` — the same shape every MCP client uses:

```json
{"servers": {"demo": {"command": ["python3", "-m", "pocket.examples.demo_server"]}}}
```

Its tools show up as `mcp__demo__<tool>`, marked "asks first". `python -m pocket mcp` writes
that starter config, connects, and makes one real call so you can see the protocol work.

## Where to read next

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | one turn traced end to end, the file map, the seven invariants |
| [docs/configuration.md](docs/configuration.md) | every environment variable, provider and MCP setting |
| [docs/teams.md](docs/teams.md) | the board: plan shape, scheduling, failure, and its limits |
| [AGENTS.md](AGENTS.md) | the rules a coding agent (or a hurried human) should follow here |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md) | the bar for a PR, the trust boundaries, what changed |

## Honest limits

- `POCKET_PROVIDER=mock` is a **rule-based stub, not a model**. It exists so the demo and the
  eval suite run offline; it proves the harness works, never that a model is smart. Point it
  at `anthropic`, `openai`, `deepseek` or `kimi` for real answers.
- Three core tools, one flagship task (scheduling). Every registered tool ships in every
  prompt, so the core stays narrow on purpose — capability arrives through MCP instead, and
  `assign_team` is off (`POCKET_TEAM=1`) until you want it.
- A team's workers are threads in this process, sharing one home and one database. There are
  no git worktrees, no per-worker sandbox, no inbox between peers and no re-planning
  mid-run: the isolation you get is the tool list. ClawTeam does the heavier version.
- Only loop calls are priced in `usage.jsonl`; the gate, triage and summariser calls are
  traced but not priced. Dollars are estimates from a small table; tokens are the truth.
- Keyword search (FTS5), not embeddings. For one person's facts, ranked keyword search is
  fast, local, and inspectable with `sqlite3`.
- The MCP client covers stdio, `server/discover`, `tools/list` and `tools/call`. Streamable
  HTTP, resources, prompts and multi round-trip input requests are not implemented — an
  `input_required` result is reported honestly instead of being half-answered.

## 中文速览

用 ~3100 行 Python 从零写的本地优先助理：把一个 Agent 真正需要的机制**每个都压进一个能读完的
文件**，并且在没有 key、没有网络、没有第三方依赖的情况下十秒内就能看到它跑起来。

关键机制：检索门控、三层记忆 + 定期固化、上下文治理（大结果外置 + 超预算折叠）、**MCP 客户端**
（2026-07-28 无状态修订，自动回退旧版握手）、权限门、受控子 Agent、**团队看板**（计划是数据不是对
话，依赖决定调度，失败即阻塞下游）、Graph 编排，以及 46 条确定性评测构成的发布门禁。

完整中文文档：**[README_CN.md](README_CN.md)**。

## Provenance

The four-pillar structure, and a few core routines (the loop's guardrails, the FTS5 query
sanitiser, the graph engine's wave scheduler), are re-implemented and trimmed from
[waku-agent](https://github.com/ShenSeanChen/waku-agent) (MIT), which is the full-featured
version — dashboard, voice and chat gateways, pluggable memory backends, an LLM-as-judge
suite. Everything under `mcp.py`, `permissions.py`, `context.py` and `subagent.py` is this
repo's own. MIT licensed.

Two more projects are read, credited and borrowed from rather than depended on:
[nanobot](https://github.com/HKUDS/nanobot) — the terminal client's `/` commands, and the
habit of a script that keeps the line count in the README honest (`core_agent_lines.sh`);
[ClawTeam](https://github.com/HKUDS/ClawTeam) — the board: a plan as data, kanban states,
dependencies that unblock themselves, and the discipline of saying "this did not happen"
instead of running a step on missing input. Neither is a dependency; both are worth reading
in full.
