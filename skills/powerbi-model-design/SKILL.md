---
name: powerbi-model-design
description: "Designs and reviews Power BI semantic models: star schema, fact and dimension grain, date tables, relationship direction and cardinality, measure tables, naming, row-level security, and calculation structure, then maps each decision to the plugin edit that implements it. Use when asked how to model data in Power BI, whether a model is designed well, how to add a date table or RLS, or how to restructure tables before building reports. Do NOT use for warehouse or lakehouse design outside Power BI (use data-modeling or warehouse-and-lakehouse)."
metadata:
  version: "1.0"
---

# Power BI model design

## Procedure

1. Read the current model (`read_model_tmdl` or `get_model_summary` + `analyze_relationships`). Name the facts (events with numbers) and the dimensions (who, what, where, when).
2. State the grain of each fact table in one sentence. If two facts have different grains, they share only conformed dimensions.
3. Check the design against the rules below, using `best_practice_scan` for the mechanical ones.
4. Propose changes one at a time (`powerbi-approved-editing`).

## Rules

- Star schema: dimensions relate to facts one-to-many, single direction from dimension to fact. Snowflake only when a dimension is large and shared.
- Relate on integer or short keys. Hide key columns and set `summarizeBy: none` on numeric keys.
- One marked date table with contiguous dates covering every fact date; relate each fact date column to it. Disable auto date/time.
- Keep measures in a measures table or the main fact table; give each a format string, a display folder when there are many, and a description for anything non-obvious.
- No many-to-many unless a bridge table is impossible; no bidirectional filters except a documented, tested reason.
- Disconnected tables (selectors, parameters) are fine; hide them from the field list or name them clearly.
- RLS: filter dimensions, not facts, and let relationships propagate. Keep role filters simple, test with View as, and never rely on hiding visuals for security.
- Names: business language, no technical prefixes in what report authors see; no leading or double spaces.

## Mapping decisions to edits

| Decision | Plugin edit |
|---|---|
| Add or fix a relationship | `plan_model_change` `add_relationship`, `set_relationship` |
| Hide keys, set summarize, sort-by, folders, formats | `set_properties` |
| Add a measure | `upsert_measure` |
| RLS role | `upsert_role` |
| Remove an unused table or column | `delete_object` (validation blocks it if a visual uses it) |
| Date table | create in Desktop (Calendar via DAX or M) or `set_partition_source` on an M-based table, then mark it as a date table in Desktop; the plan operations do not mark date tables |

## Output template

```
Current: <tables, grain, relationships, issues found>
Target design: <star diagram in text: Fact -> Dim list>
Changes (ordered): <each change, benefit, risk>
Open questions: <business definitions needed from the user>
```

## Gotchas

- A fact keyed by a text code and a dimension keyed by the same code in a different format (`2425` vs `24-25`) silently orphans rows; run `check_relationship_integrity`.
- Students or staff in more than one fact table need one conformed dimension; do not duplicate the person table per fact.
- A date table built from `CALENDARAUTO()` can span unexpected years; check min and max with `run_dax_query`.
- RLS roles created through the plugin hold filters only; assigning people to roles happens in the service.
