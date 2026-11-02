---
name: powerbi-dax-authoring
description: "Writes, reviews, explains, debugs, and optimizes DAX for Power BI measures, calculated columns, calculated tables, and RLS filters, testing candidates against the open model's engine before anything is saved. Use when asked to write a measure, fix a DAX error or a wrong number, explain what a measure does, convert slow DAX, build time intelligence, percent-of-total, ranking, or running totals, or review a model's DAX. Do NOT use for Power Query M (use powerbi-full-analysis analyze_folding_risk) or for applying the change (use powerbi-approved-editing)."
metadata:
  version: "1.0"
---

# Power BI DAX authoring

Never hand over DAX you did not test. Use the engine as the compiler.

## Procedure

```
Progress:
- [ ] 1. Learn the model: read_model_tmdl (project) or list_tables / list_columns / list_measures (open file)
- [ ] 2. Restate the business question and the grain (per what? filtered by what?)
- [ ] 3. Draft with variables; reuse existing measures instead of repeating logic
- [ ] 4. lint_dax (static) and validate_dax_live (engine compiles and evaluates)
- [ ] 5. Check the result with run_dax_query on a small slice whose answer you can verify independently
- [ ] 6. Propose the change (powerbi-approved-editing), then apply
```

For an open file, `validate_dax_live {instance, table, expression}` compiles the candidate without saving it. For a saved project, `lint_dax` only checks syntax shape and known smells, so say that the engine has not compiled it and give the user the `validate_dax_live` step after opening the file.

## Writing rules

- Variables (`VAR ... RETURN`) for any value used twice or that names a step.
- Reference measures as `[Measure]` and columns as `Table[Column]`. Never leave a column unqualified.
- `DIVIDE(a, b)` instead of `/`. Avoid `IFERROR`: it hides real faults.
- Boolean filter arguments in `CALCULATE` (`Sales[Region] = "West"`) over `FILTER(ALL(...))`.
- `SELECTEDVALUE(col, alternate)` for single-selection logic; `ISFILTERED` or `HASONEVALUE` for conditional logic.
- Format with `formatString`, not `FORMAT()`: text breaks sorting and aggregation.
- Calculated column only when the value must slice, sort, or relate; otherwise a measure.
- Time intelligence needs a marked date table with contiguous dates; check with `powerbi-model-design`.
- Patterns (percent of total, YTD, prior year, rank, running total, top N, dynamic segmentation): see [references/dax-patterns.md](references/dax-patterns.md).

## Debugging a wrong number

1. Evaluate the measure at the grain in question: `EVALUATE SUMMARIZECOLUMNS(Dim[Col], "v", [Measure])` with `run_dax_query`.
2. Evaluate its parts the same way; find the first part that differs from the expectation.
3. Suspects, in order: relationship direction or inactive relationship, filter on the wrong table, blank keys (run `check_relationship_integrity`), context transition inside an iterator, a hidden report filter (`get_report_layout`), data type or text-format key mismatch.
4. Report the cause with the evidence, not just the fix.

## Output template

```
Goal: <question the measure answers>
DAX: <final expression>
Test: <query run, result, how it was checked independently>
Lint: <findings or none>
Assumptions: <date table, relationships, filters>
```

## Gotchas

- `run_dax_query` is capped at `max_rows`; when `truncated` is true, aggregate in DAX instead of concluding anything from the rows.
- A measure that compiles can still be wrong under filters you did not test. Test at total level and at one slicer selection.
- Measure names are unique across the model; a new measure cannot reuse a column name in its own table.
- `INFO.VIEW.MEASURES()` leaves `FormatString` null and puts the format in `FormatStringDefinition` as a quoted string; the plugin handles this, but raw queries show it.
- RLS filters are DAX too: test them with View as in Desktop, since `run_dax_query` runs without role filters.
