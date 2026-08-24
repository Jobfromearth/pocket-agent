# Security

`pocket` runs on your machine, with your keys, your calendar and your memory, and
it executes tool calls a language model asked for. That makes its trust
boundaries worth stating plainly.

## Reporting a vulnerability

Open a **private** report via GitHub Security Advisories on this repository
("Security" → "Report a vulnerability"). Please do not open a public issue.
Include what you ran, what happened, and what you expected. This is a personal,
best-effort project — expect a reply in days, not hours.

## The boundaries

| Boundary | Where | What it does |
|---|---|---|
| deny list | `permissions.py` | shapes that are refused even if a human approves: recursive deletes of `~`/`/`, fork bombs, pipe-to-shell, credential paths (`.ssh/`, `.aws/credentials`, `.env`), `DROP TABLE` |
| ask-the-human | `permissions.py` | anything `risk="ask"` — every MCP tool, `delegate`, `assign_team`, `search_web`, `fetch_url` — prompts once per session, and is *refused* when nothing can ask |
| scoped registries | `tools.py`, `subagent.py`, `team.py` | a sub-agent or worker gets exactly the tools it was given, and can never delegate or start a team |
| artifact reads | `context.py` | `read_artifact` resolves names inside `.pocket/artifacts/` only — no traversal |
| outbound URLs | `web.py` | `http`/`https` only, the host must resolve to a public address, every redirect hop is re-checked, non-text responses are refused, the read is capped at 2 MB — and both web tools are `risk="ask"` |
| loopback only | `dashboard.py` | the page binds `127.0.0.1` with no flag to change it, and its SQL browser reads a five-table allow-list rather than arbitrary SQL |
| untrusted output | `injection.py` | anything from web, MCP, a sub-agent or a worker is classified, fenced as data with the finding stated, and a high-risk result escalates the next tool call to ask-the-human, once |
| chat allow-list | `telegram.py` | the bot answers listed chat ids only; with the list empty it starts, prints who wrote to it, and answers nobody |
| self-edit asks | `memory.py` | `manage_memory`, `update_soul` and `create_skill` change what this assistant will be next session, so all three are `risk="ask"` — including `manage_memory`'s harmless `search`, because a tool whose risk depends on an argument is a tool whose risk you cannot read off the registry |
| fan-out budget | `subagent.py` | one turn may start at most `POCKET_FANOUT_PER_TURN` sub-agents; a session grant means `ask` is answered once, and without a budget the model could fan out on every iteration |
| MCP isolation | `mcp.py` | third-party servers are separate stdio processes; a broken or hostile server is skipped, and its tools arrive namespaced and gated |

## What is *not* claimed

- **Tools are not sandboxed.** They are Python functions in this process. The
  protection is the deny list plus a human, not a container.
- **`delegate_task` is not sandboxed either, and it is a whole other agent.** It
  runs the command in `POCKET_CODER` as you, with your files, in the directory it
  was given, and that agent can read, write and execute. The gate is `risk="ask"`
  and a human reading the task first. Point `cwd` at a project you are willing to
  have edited, and read the manifest afterwards.
- **Prompt injection is not solved.** A web page or an MCP server can put text in
  front of the model, and `fetch_url` makes that concrete. The mitigation here is
  the permission gate, the narrow core tool set, the address guard that stops a
  fetched page from steering the next fetch inwards, and a trace you can read
  afterwards — not detection.
- **`POCKET_TRUST` is a loaded gun, and it disarms more than the prompt.** It
  skips confirmation for the names you list — including the one-shot escalation
  `injection.py` arms after a high-risk result, which works by asking. A trusted
  tool cannot be escalated. List only tools you have read.
- **The address guard is a check, not a pin.** `web.py` resolves a host, and
  urllib resolves it again when it connects; a DNS record with a zero TTL can
  answer public for the first and private for the second. Closing that means
  connecting to the validated address with the hostname in the `Host` header,
  which this client does not do.
- **The dashboard's chat endpoint is same-origin only.** It requires a JSON
  content type (so a cross-origin POST needs a preflight the browser refuses), a
  loopback `Host`, and a loopback `Origin` when one is sent. Loopback by itself
  was never a control against a page the user already has open.
- **`.pocket/` is not encrypted.** It is a directory of your data — facts,
  calendar, traces, artifacts. Back it up or delete it like any other.
- **Keys live in `.env` and in your environment.** `.gitignore` covers `.env` and
  `.pocket/`; check before you push.
