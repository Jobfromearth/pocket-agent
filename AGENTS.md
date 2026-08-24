# AGENTS.md

Guidance for AI coding agents (and humans in a hurry) working in this
repository. `CLAUDE.md` points here; there is only one copy of these rules.

## What this project is

`pocket` is a local-first personal assistant written to be **read**: every
mechanism a serious agent needs, one mechanism per file, ~3.1k lines of Python
with no runtime dependencies. Its value is legibility. A change that adds
capability but costs legibility is usually the wrong trade here.

## Commands

```bash
python -m pocket eval          # deterministic evals, must be 100% - offline and free
python -m pocket judge         # the scored suite: reply quality, gate accuracy (needs a key)
python -m pocket gate          # both suites + eval_report.json
python -m pocket demo          # a scripted tour of every pillar, offline
python -m pocket team          # the team board, three workers, offline
python -m pocket dashboard     # the browser door + the terminal, on one bus
python -m pocket telegram      # the chat door (needs a token and an allow-list)
python -m pocket mcp           # connect MCP servers and prove one call
pytest pocket/evals.py         # the same cases under pytest
ruff check pocket              # lint, same as CI
./scripts/line_budget.sh       # lines per pillar + the number the README states
```

Everything above runs offline, with no key and no install. Keep it that way.

## Rules that are not negotiable

1. **A dependency has to earn its place, and then it is a real dependency.**
   The core ran on the standard library alone until the eval and observability
   story outgrew it: DeepEval scores the judged suite and OpenTelemetry carries
   the trace, because rubric runners and wire protocols are solved problems and
   the interesting part of this repo is not either of them. The bar for a new
   one: it replaces something we would otherwise maintain badly, it is imported
   lazily at the seam that needs it, and `python -m pocket demo` and
   `python -m pocket eval` still run offline on a fresh clone.
2. **One mechanism, one file.** New machinery gets a new top-level module with a
   docstring that says what it is *for*, not what it does line by line. Don't
   create `utils.py`, and don't split a mechanism across packages.
3. **The deterministic suite stays at 100%.** Every behaviour worth defending
   gets a case in `pocket/evals.py` — they are unit tests, never model judgement.
   Anything a model scores belongs in `pocket/judge.py`, which is a threshold and
   not a gate, and which fails CLOSED: a grader that cannot grade scores 0.
4. **Failures degrade, they don't crash.** Tools return errors as text; judges
   (gate, triage, summariser, graph) fail open toward more capability, never less.
5. **Nothing that reaches outside this process runs unattended.** MCP tools,
   `delegate`, `assign_team`, `search_web`, `fetch_url`: `risk="ask"`. Anything
   new that opens a socket is `ask` too, and its guard belongs in the tool — not
   in the transport a test can replace. A tool whose output is somebody else's
   text needs an `origin` outside `core`, or `injection.py` will not screen it.
6. **Nothing is deleted to save context.** Offload it or summarise it; the row in
   `state.db` stays.
7. **Docs are part of the change.** If you add a knob, it goes in
   `docs/configuration.md` and `.env.example`. If you change the line count, run
   `./scripts/line_budget.sh` and update the number in the README — an eval
   asserts the README is telling the truth.

## Style

- Comments explain *why a decision was made*, not what the next line does. If a
  line needs a comment to be understood, prefer rewriting the line.
- Names read as prose: `should_retrieve`, `offload_if_large`, `worker_tools`.
- 100 columns (`ruff` config in `pyproject.toml`), `from __future__ import annotations`
  at the top of every module.
- Prefer a plain function over a class; prefer a dataclass over a config object;
  prefer an injected callable over a subclass hook.

## Where things live

See [docs/architecture.md](docs/architecture.md) for the file map and the turn
lifecycle. The short version: `agent.py` wires everything, `loop.py` is the
loop, and every other file is one mechanism named after itself.

## Provenance

Structure and a few core routines are re-implemented from
[waku-agent](https://github.com/ShenSeanChen/waku-agent) (MIT). The terminal
`/` commands follow [nanobot](https://github.com/HKUDS/nanobot); the team board
follows [ClawTeam](https://github.com/HKUDS/ClawTeam). Keep those credits in the
README when you touch the parts they inspired.
