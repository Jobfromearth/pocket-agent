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
| ask-the-human | `permissions.py` | anything `risk="ask"` — every MCP tool, `delegate`, `assign_team` — prompts once per session, and is *refused* when nothing can ask |
| scoped registries | `tools.py`, `subagent.py`, `team.py` | a sub-agent or worker gets exactly the tools it was given, and can never delegate or start a team |
| artifact reads | `context.py` | `read_artifact` resolves names inside `.pocket/artifacts/` only — no traversal |
| MCP isolation | `mcp.py` | third-party servers are separate stdio processes; a broken or hostile server is skipped, and its tools arrive namespaced and gated |

## What is *not* claimed

- **Tools are not sandboxed.** They are Python functions in this process. The
  protection is the deny list plus a human, not a container.
- **Prompt injection is not solved.** A web page or an MCP server can put text in
  front of the model. The mitigation here is the permission gate, the narrow core
  tool set, and a trace you can read afterwards — not detection.
- **`POCKET_TRUST` is a loaded gun.** It skips the prompt for the names you list;
  list only tools you have read.
- **`.pocket/` is not encrypted.** It is a directory of your data — facts,
  calendar, traces, artifacts. Back it up or delete it like any other.
- **Keys live in `.env` and in your environment.** `.gitignore` covers `.env` and
  `.pocket/`; check before you push.
