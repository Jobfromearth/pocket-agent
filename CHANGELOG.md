# Changelog

## Unreleased

**Consolidation has a history you can walk back.** `dream.py` — every run that
writes anything records the fact ids and episode it created, how many exchanges
it read, and MEMORY.md as it stood before, under an eight-character sha.
`/dream` runs one now, `/dream-log` lists them, `/dream-log <sha>` shows what one
decided, `/dream-restore <sha>` retracts exactly the facts that run added.

Consolidation is the one background job here that can quietly make the assistant
wrong about your life — a model deciding, unsupervised, what you will be
remembered as believing — and it had left no way to see or undo that.

`restore` is not a snapshot rollback: a snapshot would also undo what you told
the assistant since. It retracts that run's facts and nothing else, and it
retracts rather than deletes. The prompt that guides it is `.pocket/dream.md`,
written on first run and read on every one after, so what counts as worth
remembering is a file you edit rather than a constant you fork the repo to
change — and an edit that loses `{log}` falls back instead of sending a prompt
with no input in it.

**The assistant can edit its own memory.** `manage_memory` (search, correct,
forget), `update_soul` (a standing rule, appended under a "Learned rules"
heading in the STABLE half of the prompt so it does not cost the cache every
turn) and `create_skill` (a SKILL.md, with the catalog reloaded on the spot).
All three are `risk="ask"`: they change what this assistant will be next
session, which is a different kind of power from booking a meeting. Even
`manage_memory`'s harmless `search` asks, because a tool whose risk depends on
one of its arguments is a tool whose risk you cannot read off the registry.

`forget` retracts rather than deletes: the fact leaves search and MEMORY.md,
the row stays in `state.db` marked `forgotten`. "Nothing is deleted" had to
survive a tool whose whole job is forgetting. `correct` maintains the FTS5
shadow by hand, because a content-backed index that is not told about an UPDATE
keeps answering with text the table no longer holds.

`db.py` gains the smallest migration that can add a column to a database an
older version made, and the rule that keeps it small: add, never rename, never
drop.

**Fixed:** the argument self-check ran before the permission gate, so a call
that was going to be refused could come back as a schema error instead. The
deny list has to win over everything.

**The prompt cache is actually on now.** `context.py` had claimed to be
cache-friendly since the beginning and no breakpoint had ever been sent, so the
claim was about prefix stability and nothing else. Two things were missing and
the second is the one that mattered: a `cache_control` marker, and a prefix
worth marking. `Session.build_system_parts` now returns [stable, per-turn] —
persona, provider and the skill catalog before the breakpoint, the clock and the
gate's retrieval after it. Marking a prefix that changes every minute is worse
than marking nothing, because a cache write costs more than not caching.

`usage.jsonl` records `cached` and `cache_written` per call, `estimate_cost`
prices them at Anthropic's 0.1x read / 1.25x write, and `spend()` reports a hit
rate. Providers that do not report cache tokens read 0 — an unknown rate is not
a good one.

**Fan-out is now something the model is told to do.** `delegate` and
`assign_team` were ordinary tools with good descriptions and nothing telling the
model *when* the shape of a request calls for one — so it almost never reached
for either. SOUL.md now carries the routing rule: do it yourself; one
self-contained sub-task whose middle you do not need to see goes to `delegate`;
two or more independent or ordered sub-tasks go to `assign_team` with `needs`.
The old "call each tool at most once per request" rule was written to stop
double-booking a meeting and was braking multi-step work as a side effect; it
now forbids repeating a call *with the same arguments*, which is what it always
meant. `pocket tools` also stops quietly disagreeing with a real chat: it says
out loud that `assign_team` is shown for reference and needs `POCKET_TEAM=1`.

**Context governance, in four steps instead of two.** The window is now checked
before *every* model call rather than once when a turn starts — a turn that
calls eight tools appends eight results after the only check a start-of-turn
budget would ever do. `fit_for_model` shortens old tool results **in place**, so
no message is removed and a `tool_use` can never lose its `tool_result`; the
three most recent stay whole. If a provider still refuses the prompt as too
long, the turn shortens hard and retries **once**, then raises. And the
compacted message now names `read_history`, a new tool over `chat_log`: the
record always held every message, but until now only a human with `sqlite3`
could reach it, which made "nothing is deleted" true for the wrong reader.

Order of the four is the point: offload, fit, react, compact. Only the last
costs a model call and only the last loses detail.

**Skills, in two levels.** `skills.py` — the catalog (name + description) always
ships in the system prompt; a body arrives as its own message, never folded into
the prompt, either because the matcher was confident or because the model called
the new `read_skill(name)`. The matcher now tokenises unsegmented scripts, which
it did not before: it had been silently dropping every skill from every turn in
one.

**Hooks.** `hooks.py` — five named moments (`turn_start`, `system_built`,
`before_tool`, `after_tool`, `turn_end`). A hook may veto a tool call or rewrite
its result; the first opinion wins, and one that raises is dropped rather than
obeyed.

**Prompt injection screening.** `injection.py`, registered as a hook pair.
Untrusted output is classified, fenced as data with the finding named in the
open, and a high-risk result arms exactly one escalation: the next tool call
asks a human even if it normally runs unattended. `POCKET_SCREEN=0` turns it off.

**Argument self-check.** A missing required argument is now an error the model
can read and retry from, instead of a `TypeError` out of `fn(**args)`.

**A third door.** `python -m pocket telegram`, under a hundred lines, with
`POCKET_TELEGRAM_ALLOW` as a mandatory allow-list of chat ids.

**Many doors, one conversation.** `bus.py` — every gateway submits to one
worker, so a message typed in a browser tab and one typed in the terminal land
in the same `Session`, in the order the bus took them, and cannot interleave
into one context window. Events are published to every subscriber, so a turn
started in one door streams to all of them. A subscriber that raises is dropped
rather than allowed to take the turn down.

**A browser door.** `python -m pocket dashboard` serves one static page on
`127.0.0.1:7777` (no build step) with seven panels — Overview, Chat, Loop (the
live tape over SSE), Memory, Tools, Data, Ops. Every panel is a projection of
something already on disk, so closing the tab loses nothing. The SQL browser
reads a five-table allow-list.

**Judged evals.** `judge.py` — the second suite, kept away from the first on
purpose. Three reply-quality cases scored 0-1 against a written criterion
(threshold 0.6), and twelve retrieval-gate cases scored by *cost-weighted*
accuracy: a missed retrieval is priced at 4x a needless one, because it answers
confidently from nothing. `python -m pocket judge` runs it alone, `python -m
pocket gate` runs both and writes `eval_report.json` plus an appended
`eval_runs.jsonl`. With no key the judged suite is `skipped`, never `pass`. One
inversion from the rest of the repo, stated in the file: a broken judge here
fails **closed** — in an eval, "I could not tell" is a failure, not a pass.

**OTLP mirror.** With `OTEL_EXPORTER_OTLP_ENDPOINT` set, every trace event is
also exported as a span (`agent_run` per turn, one child per event) for Phoenix,
Langfuse or any other receiver. No per-vendor adapter, an optional dependency
(`pocket-agent[tracing]`), and a broken exporter never costs a turn.


**The web.** `web.py` — `search_web` (DuckDuckGo's HTML endpoint, no key) and
`fetch_url` (one page, stripped to prose). One guarded opener behind both:
http/https only, the host must resolve to a public address, every redirect hop
is re-checked, a non-text response is refused, the read is capped at 2 MB. The
address check lives in the tool rather than in the transport, because the
transport is the seam the eval suite replaces. Both are `risk="ask"`;
`POCKET_WEB=0` removes them from every prompt.

**Evals:** 46 → 100 deterministic cases.

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
