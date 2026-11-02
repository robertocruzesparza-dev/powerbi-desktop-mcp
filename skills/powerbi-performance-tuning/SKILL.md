---
name: powerbi-performance-tuning
description: "Finds and removes the causes of slow or oversized Power BI models and reports: heavy columns, unused fields, query folding breaks, repeated SharePoint fetches, expensive DAX, auto date/time tables, bidirectional relationships, and crowded report pages, ranked by expected payoff. Use when asked to speed up a dashboard, shrink a file, reduce refresh time, cut visual load time, or prepare a model for more users. Do NOT use to explain one wrong number (use powerbi-troubleshooting)."
metadata:
  version: "1.0"
---

# Power BI performance tuning

Measure before changing, change one thing, measure again. The plugin gives static and storage evidence; timings need Desktop's own tools.

## Procedure

```
Progress:
- [ ] 1. Name the slow thing: refresh, opening, a page, one visual, one measure
- [ ] 2. Baseline evidence (below)
- [ ] 3. Rank candidates by expected payoff and risk
- [ ] 4. One change via powerbi-approved-editing; re-measure
```

## Evidence by slow thing

| Slow thing | Collect with | What to look for |
|---|---|---|
| File size, memory | `analyze_model_size`, `find_unused_fields` or `find_unused_fields_project` | biggest tables and columns; unused high-cardinality text columns, GUIDs, timestamps with seconds |
| Refresh | `analyze_folding_risk`, `analyze_sql_queries`, `analyze_sharepoint_patterns`, `read_mashup_traces`; offline: `analyze_project_queries` | native SQL (never folds), `Table.Buffer`, filters after folding ended, the same SharePoint list loaded by several queries, `SELECT *`, failed requests |
| Model structure | `best_practice_scan` | bidirectional or many-to-many relationships (BPA001/002), auto date/time tables (BPA033), calculated columns that are plain aggregations (BPA005), visible keys |
| Measure speed | `lint_dax`, then time candidates | DAX smells (IFERROR, FILTER(ALL), nested CALCULATE, COUNTROWS(FILTER)), iterators over large tables |
| Page load | `get_report_definition` or `get_report_layout` | visuals per page (PBIR030), many slicers, tables with many columns, cross-highlighting everywhere |

Timing needs the user: Performance Analyzer in Desktop (View > Performance analyzer) for visuals, or DAX query timings. Do not claim a measured speed-up the plugin did not measure; say "expected to reduce" and name the evidence.

## Ranked fix list (typical order of payoff)

1. Remove or hide columns nothing uses, and drop timestamps to dates where seconds are unneeded (storage).
2. Turn off auto date/time and use one marked date table.
3. Move filtering and column removal upstream so the source does the work; restore folding by reordering steps.
4. Replace repeated SharePoint pulls with one staging query referenced by the others.
5. Switch bidirectional relationships to single direction.
6. Rewrite smelly measures (see `powerbi-dax-authoring`).
7. Reduce visuals per page; split rarely used detail to a drillthrough page.
8. Only then consider aggregations, incremental refresh, or composite models (design decision for the user).

## Output template

```
Target: <slow thing and how it was observed>
Evidence: <tool outputs, numbers>
Recommendations (ranked): <change, expected effect, risk, how to verify>
Not measured: <timings that need Performance Analyzer or the source system>
```

## Gotchas

- `find_unused_fields` results are leads: tooltips, Q&A, bookmarks created in the service, and other reports on the same model are invisible.
- Folding verdicts are estimates from M text; confirm with View Native Query in Power Query.
- Deleting columns changes the model: validate with `validate_project` and check report fields before applying.
- Incremental refresh keeps older partitions stale until their turn; mention it before promising fresher data.
