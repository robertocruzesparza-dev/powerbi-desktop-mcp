# Copilot instructions

Follow [AGENTS.md](../AGENTS.md) at the repository root: it explains which MCP tool layer to use for Power BI work
and the rules for approved, reversible edits. Skills are in `skills/` (copy to `.github/skills/` in your own project
with `python scripts/install_skills.py --target copilot --project <folder>`).

When changing this repository, run `python -m pytest tests -q` and `python -m unittest discover -s copilot/tests`.
