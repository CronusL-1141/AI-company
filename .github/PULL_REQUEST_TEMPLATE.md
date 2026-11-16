## Problem

<!-- What was wrong or missing, as a user meets it. Link the issue or discussion (Closes #123). -->

## Cause

<!-- For a fix: the root cause, not the symptom. For a feature: why the current design cannot do it. -->

## Change

<!-- What you changed and why this way. Mention anything you refactored first, and anything you deliberately left out. -->

## Verification

<!-- How you know it works: the tests you added (red before the change, green after), what else you ran, and how you reproduced the problem end to end. -->

- [ ] `python3 -m pytest tests/` passes
- [ ] `bash scripts/check_invariants.sh` passes

## Checklist

- [ ] A file changed under `src/aiteam/hooks/` has its twin under `plugin/hooks/` changed byte for byte (invariant I1)
- [ ] Tool, endpoint or page counts in the README were updated from a fresh measurement, or are untouched
- [ ] The Dashboard bundle in `plugin/dashboard-dist/` was rebuilt, or no frontend file changed
- [ ] No tokens, personal paths or private names in code, tests, docs or this description
