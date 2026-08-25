# Contributing

Thanks for your interest. This project is heavily dogfooded and moves fast; small, focused PRs work best.

## Dev setup

```bash
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company
python3 install.py   # editable source install: MCP + hooks + API auto-configured
```

Python >= 3.11 (3.12 recommended). The Dashboard needs Node >= 20: `cd dashboard && npm install && npm run dev`.

## Running checks

```bash
python3 -m pytest tests/              # full unit/integration suite
bash scripts/check_invariants.sh      # machine-checked release invariants (I1-I14)
cd dashboard && npx eslint src        # frontend lint
```

CI runs all three. `check_invariants.sh` is the gate that surprises people - it verifies things like version lockstep and doc numbers against measured reality, so a failing invariant usually means a missed sync step, not a broken build.

## Two rules that are deliberate (please do not "fix" them)

- **Hook files exist in twin copies**: `src/aiteam/hooks/` and `plugin/hooks/` must stay byte-identical (invariant I1). Edit one, copy to the other in the same commit. This is not accidental duplication.
- **No virtualenv**: four kinds of processes share one interpreter by design. Use system Python + `sys.executable`; do not introduce venv isolation.

More context on these and other deliberate decisions lives in `CLAUDE.md` and `docs/architecture.md`.

## Pull requests

- Target `master`, keep the diff focused on one concern.
- Add or update tests for behavior changes; the hook suite (`tests/test_workflow_reminder.py`) uses real temporary git repos rather than mocks - follow that pattern.
- Numbers in the README (tool counts, test counts) are machine-checked against measurements. Update them only with a fresh measurement, never from memory.
- Clear commit messages in English or Chinese are both fine.
