# Changelog

## 0.3.0

**Teams.** `team.py` — several workers over one shared board, inspired by
[ClawTeam](https://github.com/HKUDS/ClawTeam). A plan is data (keys, tools,
`needs`), the DAG is scheduled by the existing graph engine so independent tasks
run in one wave, a worker is handed only the results of what it depends on, and a
failed worker leaves its dependents `blocked` instead of running them on missing
input. The board is a `tasks` table in `state.db`. Off by default: `POCKET_TEAM=1`
registers `assign_team` (`risk="ask"`), and `python -m pocket team` runs a
three-worker plan with no model deciding anything.

**Terminal `/` commands**, following [nanobot](https://github.com/HKUDS/nanobot):
`/help`, `/tools`, `/context`, `/memory`, `/board`, `/new`. None of them reach a
model — what is in context and what can run are answers the harness already has.

**Fixed:** `state.db` now opens in autocommit mode. Python's implicit transaction
is per-connection, not per-thread, so with parallel workers one worker's commit
could end another's transaction and kill it with "no transaction is active".

**Docs and repo structure:** `docs/` (architecture, configuration, teams),
`AGENTS.md` + `CLAUDE.md`, `CONTRIBUTING.md`, `SECURITY.md`, issue and PR
templates, `scripts/line_budget.sh`, a full Chinese `README_CN.md`, and CI across
Python 3.11–3.13.

**Evals:** 35 → 46 deterministic cases, including one that asserts the line count
in the README is still true.

## 0.2.0

MCP client for the 2026-07-28 stateless revision with automatic fallback to the
legacy handshake, the permission gate, context offloading and compaction,
sub-agents, and the triage graph.

## 0.1.0

The loop, three-layer memory with a retrieval gate, the calendar tools, the
trace and spend ledger, and the first deterministic suite.
