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
python -m pocket eval     # 35 deterministic checks + the release gate, in under a second
python -m pocket mcp      # start an MCP server and call a tool through it, no model involved
python -m pocket tools    # what the model can call — and what it may do without asking
python -m pocket          # chat for real (put a key in .env)
```

The core imports **stdlib only**. `anthropic` / `openai` load lazily, and only if you
point it at that provider.

## What's inside

| Pillar | Files | What lives there |
|---|---|---|
| **Harness** | `config.py` `session.py` `agent.py` `__main__.py` | working-memory assembly, wiring, the terminal gateway |
| **Loop** | `loop.py` `models.py` `tools.py` | reason→act→observe with two guardrails; 2 wire formats behind one loop |
| **Memory** | `memory.py` `db.py` | semantic (FTS5) / episodic / procedural, a retrieval gate, consolidation |
| **Context** | `context.py` | large results offloaded to disk; history compacted when it outgrows its budget |
| **Reach** | `mcp.py` `subagent.py` | MCP tools from other people's servers; delegation to a scoped sub-agent |
| **Safety** | `permissions.py` | deny list, ask-the-human, per-session grants — refusals come back as text |
| **Graph** | `graph.py` | structure *around* the loop: parallel nodes, code routers, fail-open |
| **Ops** | `trace.py` `evals.py` | JSONL trace, spend ledger, deterministic evals, release gate |

State lives in `.pocket/`: `state.db` (SQLite + FTS5), `calendar.ics`, `MEMORY.md`,
`artifacts/`, `traces/<date>.jsonl`, `usage.jsonl`. All of it yours to open.

## Eight decisions worth defending

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

8. **Deterministic evals and judged evals never share a file.** "Did `create_event` fire with
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

## Connecting an MCP server

`.pocket/mcp.json` — the same shape every MCP client uses:

```json
{"servers": {"demo": {"command": ["python3", "-m", "pocket.examples.demo_server"]}}}
```

Its tools show up as `mcp__demo__<tool>`, marked "asks first". `python -m pocket mcp` writes
that starter config, connects, and makes one real call so you can see the protocol work.

## Honest limits

- `POCKET_PROVIDER=mock` is a **rule-based stub, not a model**. It exists so the demo and the
  eval suite run offline; it proves the harness works, never that a model is smart. Point it
  at `anthropic`, `openai`, `deepseek` or `kimi` for real answers.
- Three core tools, one flagship task (scheduling). Every registered tool ships in every
  prompt, so the core stays narrow on purpose — capability arrives through MCP instead.
- Only loop calls are priced in `usage.jsonl`; the gate, triage and summariser calls are
  traced but not priced. Dollars are estimates from a small table; tokens are the truth.
- Keyword search (FTS5), not embeddings. For one person's facts, ranked keyword search is
  fast, local, and inspectable with `sqlite3`.
- The MCP client covers stdio, `server/discover`, `tools/list` and `tools/call`. Streamable
  HTTP, resources, prompts and multi round-trip input requests are not implemented — an
  `input_required` result is reported honestly instead of being half-answered.

## 中文速览

用 ~2600 行 Python 从零写的本地优先助理：目标是把一个 Agent 真正需要的机制**每个都压进一个能读完的
文件**，并且在没有 key、没有网络、没有第三方依赖的情况下十秒内就能看到它跑起来。

关键机制：检索门控（先判断这轮要不要记忆，失败一律 fail-open）、三层记忆 + 每 N 轮自动固化、
上下文治理（大结果外置为 artifact + 指针、超预算时旧对话折叠为摘要）、**MCP 客户端（按 2026-07-28
无状态修订实现，并保留对旧版 initialize 握手的自动回退）**、权限门（第三方工具默认询问人类，拒绝以
文本回给模型而不是抛异常）、受控子 Agent（不移交控制权、只回传结果、不可再委派）、Graph 编排
（并行 + 代码路由 + 全链路 fail-open），以及 35 条确定性评测构成的发布门禁。

## Provenance

The four-pillar structure, and a few core routines (the loop's guardrails, the FTS5 query
sanitiser, the graph engine's wave scheduler), are re-implemented and trimmed from
[waku-agent](https://github.com/ShenSeanChen/waku-agent) (MIT), which is the full-featured
version — dashboard, voice and chat gateways, pluggable memory backends, an LLM-as-judge
suite. Everything under `mcp.py`, `permissions.py`, `context.py` and `subagent.py` is this
repo's own. MIT licensed.
