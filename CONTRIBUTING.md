# Contributing

Thanks for being here. `pocket` is a teaching-shaped codebase: it exists so a
person can read every mechanism an agent needs in one evening. That goal shapes
what gets merged.

## The bar

A change is easy to merge when it:

- keeps the core **dependency-free** and offline-runnable (`python -m pocket demo`
  and `python -m pocket eval` must work on a fresh clone with no key);
- adds **one mechanism in one file**, with a docstring that explains the decision
  behind it;
- comes with **deterministic evals** in `pocket/evals.py` for the behaviour it
  claims;
- leaves the release gate at **100%**.

A change is hard to merge when it adds a dependency, spreads one idea across
several files, adds a knob nobody asked for, or makes a mechanism shorter by
making it harder to explain.

## Before you open a PR

```bash
python -m pocket eval          # must print "release gate: PASS"
ruff check pocket              # must be clean
./scripts/line_budget.sh       # if the total moved, update the README number
```

CI runs exactly these, on Python 3.11–3.13. There are no secrets, no network,
and no flaky tests — if CI is red, it is your change.

## Writing evals

`pocket/evals.py` is the whole suite, on purpose: it ships inside the package, so
`python -m pocket eval` proves an *installed* copy, not just a checkout.

- Deterministic only. "Did the right tool fire with the right arguments and did
  the row land?" is a unit test. "Was the reply good?" is a judged eval and does
  not belong in this file.
- Name the case after the behaviour you are defending
  (`test_a_failed_worker_blocks_its_dependents_instead_of_guessing`), and put the
  reason in the docstring.
- Use `build_agent(...)`, which gives you a throwaway home, the scripted offline
  model, and injectable seams for the human and the database.

## Commit and PR shape

- One idea per PR. If the diff needs two paragraphs to explain, it is two PRs.
- Say in the description what invariant the change protects or relaxes
  (see the list in [docs/architecture.md](docs/architecture.md)).
- Update the docs in the same PR: `docs/configuration.md` for a knob,
  `docs/` + README for a mechanism, `.env.example` for anything env-driven.

## Reporting security issues

Please do not open a public issue for a vulnerability — see
[SECURITY.md](SECURITY.md).
