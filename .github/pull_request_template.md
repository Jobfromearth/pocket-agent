## What this changes

<!-- One paragraph. What mechanism does this add, fix or remove? -->

## Which invariant it touches

<!-- See docs/architecture.md. Does it protect one, relax one, or add one? -->

## Checklist

- [ ] `python -m pocket eval` prints `release gate: PASS`
- [ ] `ruff check pocket` is clean
- [ ] the core still imports the standard library only, and still runs with no key
- [ ] new behaviour has a deterministic case in `pocket/evals.py`
- [ ] docs updated (`docs/configuration.md` for a knob, `README` / `docs/` for a mechanism, `.env.example` for env vars)
- [ ] if the line total moved: `./scripts/line_budget.sh` and the README number agree
