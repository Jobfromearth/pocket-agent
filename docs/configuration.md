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
| `POCKET_SUBAGENTS` | `1` | register `delegate` (one sub-task, one sub-agent) |
| `POCKET_TEAM` | `0` | register `assign_team` (several workers over one board) |
| `POCKET_GRAPH_WORKFLOWS` | `0` | put the triage graph in front of the loop |
| `POCKET_TRUST` | — | comma-separated tool names pre-approved for the session |
| `POCKET_HOME` | `.pocket` | where all state lives |

`POCKET_TEAM` and `POCKET_GRAPH_WORKFLOWS` are off by default for opposite
reasons: every registered tool ships in *every* prompt, so a team is opted into;
and the graph is a front door you should be able to turn off to see the loop
underneath.

## MCP servers

`.pocket/mcp.json` — the same shape every MCP client uses:

```json
{"servers": {"demo": {"command": ["python3", "-m", "pocket.examples.demo_server"],
                      "timeout": 20.0}}}
```

Tools arrive as `mcp__<server>__<tool>`, marked `risk="ask"`. A server that
fails to start is reported once and skipped — never fatal. `python -m pocket mcp`
writes a starter config and proves one call without a model involved.

## Permissions

`permissions.py` holds a short deny list that wins over any confirmation
(recursive deletes, fork bombs, pipe-to-shell, credential paths, `DROP TABLE`).
Everything marked `risk="ask"` prompts once per session; `POCKET_TRUST` skips
the prompt for names you already trust. With no way to ask a human — a cron run,
a bot — an `ask` tool is refused rather than run.
