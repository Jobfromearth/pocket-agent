# Configuration

Every knob is an environment variable read once into `Settings` (`config.py`).
`.env` is loaded on startup, and a real environment variable always wins over
`.env`, so `POCKET_PROVIDER=openai python -m pocket` overrides the file.

## Brain

| Variable | Default | What it does |
|---|---|---|
| `POCKET_PROVIDER` | first key found, else `mock` | `anthropic` · `openai` · `deepseek` · `kimi` · `mock` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `MOONSHOT_API_KEY` | — | the key for that provider |
| `POCKET_API_KEY` | — | overrides whichever key the provider would use |
| `POCKET_BASE_URL` | provider default | point at a gateway or a local server |
| `POCKET_MODEL` | provider default | the model that runs the loop |
| `POCKET_SMALL_MODEL` | provider default | the cheap one: gate, triage, consolidation, compaction |

On Anthropic the system prompt is sent as two blocks with a cache breakpoint after
the first: persona, provider and the skill catalog are stable for a session, so they
sit before it; the clock and whatever the retrieval gate pulled in change every turn,
so they sit after. A prefix that changes every turn is worse than no breakpoint at
all — a cache write costs more than not caching. `usage.jsonl` records `cached` and
`cache_written` per call, and `pocket trace` prints the hit rate.

With no key anywhere, the provider is `mock`: a rule-based stub so the demo and
the eval suite run offline. It proves the harness works, never that a model is
smart.

## Loop and context

| Variable | Default | What it does |
|---|---|---|
| `POCKET_MAX_ITERATIONS` | `8` | hard stop for one turn — guardrail 2 |
| `POCKET_MAX_TOKENS` | `4096` | per model call |
| `POCKET_HISTORY_TURNS` | `8` | how many past turns enter the prompt |
| `POCKET_TOOL_RESULT_LIMIT` | `2000` | above this, a result is offloaded to `artifacts/` |
| `POCKET_CONTEXT_BUDGET` | `12000` | chars of conversation before the oldest turns fold into a summary |

## Memory

| Variable | Default | What it does |
|---|---|---|
| `POCKET_CONSOLIDATE_EVERY` | `3` | exchanges between consolidation runs |
| `POCKET_RETRIEVAL_TOP_K` | `4` | facts retrieved when the gate says yes |

## Capability

| Variable | Default | What it does |
|---|---|---|
| `POCKET_SELF_EDIT` | `1` | register `manage_memory`, `update_soul`, `create_skill` (all `risk="ask"`) |
| `POCKET_WEB` | `1` | register `search_web` and `fetch_url` (both `risk="ask"`) |
| `POCKET_SUBAGENTS` | `1` | register `delegate` (one sub-task, one sub-agent, in-process) |
| `POCKET_CODER_TOOL` | `1` | register `delegate_task` (a coding agent, out of process) |
| `POCKET_CODER` | `pi -p {task} -a --no-session --mode json` | the command `delegate_task` runs; `{task}` is where the instruction goes. stdin is closed, so the agent must be able to write without asking — Claude Code needs `claude -p --permission-mode acceptEdits {task}` |
| `POCKET_FANOUT_PER_TURN` | `3` | how many sub-agents one turn may start, across all three fan-out tools |
| `POCKET_TEAM` | `0` | register `assign_team` (several workers over one board) |
| `POCKET_GRAPH_WORKFLOWS` | `0` | put the triage graph in front of the loop |
| `POCKET_TRUST` | — | comma-separated tool names pre-approved for the session |
| `POCKET_HOME` | `.pocket` | where all state lives |

`POCKET_TEAM` and `POCKET_GRAPH_WORKFLOWS` are off by default for opposite
reasons: every registered tool ships in *every* prompt, so a team is opted into;
and the graph is a front door you should be able to turn off to see the loop
underneath.

## The web

`search_web` and `fetch_url` (`web.py`) are the only tools that leave this
machine, so they carry their own rules — and none of these is a knob:

| Rule | What it means |
|---|---|
| scheme | `http` and `https` only — never `file://`, `data:` or anything else |
| address | the host must resolve to a **public** address; loopback, private, link-local and reserved ranges are refused before a socket opens |
| redirects | every hop is checked again, because a public host may answer `302` with `127.0.0.1` |
| type | a non-text response is refused, not decoded into the prompt |
| size | the read is capped at 2 MB while it streams |
| timeout | 20 seconds for the whole call |

Both are `risk="ask"`, so the first call in a session prompts; `POCKET_WEB=0`
removes them entirely, and `POCKET_TRUST=search_web` skips the prompt once you
have read the file. Search scrapes DuckDuckGo's HTML endpoint — no key, no
account — and a page that comes back is *information*, never instructions: the
model reads it, and the trace records what was read.

## Doors

| Variable | Default | What it does |
|---|---|---|
| `POCKET_DASHBOARD_PORT` | `7777` | where `python -m pocket dashboard` listens, on `127.0.0.1` only |
| `TELEGRAM_BOT_TOKEN` | — | the bot token from @BotFather |
| `POCKET_TELEGRAM_ALLOW` | — | comma-separated chat ids the bot will answer; empty answers nobody |

Every gateway meets at `bus.py`: messages carry a `source`, turns are serialised
by one worker so two doors cannot interleave into one context window, and events
are published to every subscriber. The dashboard binds loopback and there is no
flag to change that — it exposes memory, a read-only SQL browser over five
tables, and a chat box with your tools behind it.

## Tracing and evals

| Variable | Default | What it does |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | mirror the JSONL trace as OpenTelemetry spans (Phoenix, Langfuse, any OTLP receiver) |

The JSONL trace and the spend ledger are always written and need nothing
installed. Setting the endpoint above additionally exports one `agent_run` span
per turn with a child span per event. The OpenTelemetry SDK and the OTLP/HTTP
exporter are hard dependencies, so nothing extra to install — point it at port
4318, which is OTLP/HTTP; 4317 is gRPC and this exporter does not speak it.
There is no per-vendor adapter and there should not be: every receiver speaks the same protocol. A missing dependency or
an unreachable collector is recorded and skipped — it never costs a turn.

Evals come in two suites that never share a runner:

```bash
python -m pocket eval     # deterministic only: offline, free, 100% required
python -m pocket judge    # scored only: reply quality + gate accuracy (needs a key)
python -m pocket gate     # both, then write eval_report.json + eval_runs.jsonl
```

With no key the judged suite is reported as `skipped`, and the gate still passes
on the deterministic suite alone. `skipped` is never `pass`.

## MCP servers

`.pocket/mcp.json` — the same shape every MCP client uses:

```json
{"servers": {
  "demo":   {"command": ["python3", "-m", "pocket.examples.demo_server"], "timeout": 20.0},
  "remote": {"url": "https://example.com/mcp",
             "headers": {"Authorization": "Bearer ..."}}}}
```

`command` means stdio (a child process); `url` means Streamable HTTP (a server
somebody else runs). One key decides, because a config that needs a `transport`
field is a config with two ways to be wrong. An HTTP endpoint is deliberately
*not* behind `web.py`'s public-address guard: that guard exists because a model
can be talked into fetching a URL a web page named, while an MCP endpoint is a
URL you wrote into this file — and the most ordinary one is
`http://127.0.0.1:3000`.

Tools arrive as `mcp__<server>__<tool>`, marked `risk="ask"`. A server that
fails to start is reported once and skipped — never fatal. `python -m pocket mcp`
writes a starter config and proves one call without a model involved.

## Screening untrusted output

| Variable | Default | What it does |
|---|---|---|
| `POCKET_SCREEN` | `1` | classify output from web, MCP, sub-agent and team tools; fence what looks like an injection and escalate the next call |

`injection.py` scores untrusted output against a short list of shapes, wraps
anything suspicious in a banner naming it as data, and arms one escalation after
a high-risk finding: the next tool call asks a human even if it normally runs
unattended. Escalation is one-shot on purpose — a permanent downgrade trains you
to click through every prompt, and a prompt everybody clicks through is not a
control.

## Permissions

`permissions.py` holds a short deny list that wins over any confirmation
(recursive deletes, fork bombs, pipe-to-shell, credential paths, `DROP TABLE`).
Everything marked `risk="ask"` prompts once per session; `POCKET_TRUST` skips
the prompt for names you already trust. With no way to ask a human — a cron run,
a bot — an `ask` tool is refused rather than run.
