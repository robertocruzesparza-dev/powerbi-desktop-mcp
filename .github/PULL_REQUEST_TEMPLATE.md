## What and why

## How I tested
- [ ] `python -m unittest discover -s copilot/tests` passes
- [ ] Live-model changes tried against Power BI Desktop (version: ___) or marked "not tested live"

## Checklist
- [ ] No real data, `.pbix` files, tenant/client IDs, tokens or tunnel URLs in the diff
- [ ] Editing changes keep: edit-folder guard, code never returned to the caller, backup, verify, rollback (tests added)
- [ ] Tool names/descriptions updated in `docs/TOOLS.md`
- [ ] `CHANGELOG.md` updated
