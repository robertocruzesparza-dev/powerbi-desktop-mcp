# PBIR visual reference

Contents: folder layout, visual types and roles, field reference JSON, validation rules.

## Folder layout (report `definition/`)

```
version.json                      report.json  (theme, report-level filters, settings)
reportExtensions.json             report-level measures (optional)
pages/pages.json                  pageOrder, activePageName
pages/<pageId>/page.json          name (= folder), displayName, width, height, visibility
pages/<pageId>/visuals/<visualId>/visual.json
bookmarks/<id>.bookmark.json      bookmarks/bookmarks.json
```

`name` inside each file must equal its folder name (PBIR014, PBIR021). Ids are 20 hex characters.

## Visual types and query roles (`add_visual`)

| visual_type | Roles (first is the default) |
|---|---|
| card, multiRowCard, slicer, tableEx | Values |
| pivotTable | Rows, Columns, Values |
| clusteredBarChart, clusteredColumnChart, stackedBarChart, stackedColumnChart, lineChart, areaChart | Category, Y, Series |
| pieChart, donutChart, waterfallChart, funnel | Category, Y |
| scatterChart | Category, X, Y, Size |
| gauge | Y |
| kpi | Indicator, TrendLine, Goal |
| treemap | Group, Values |
| map | Category, Size |
| textbox, shape, image, actionButton | none (no data fields) |

## Field reference JSON

```json
{"Column":  {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Region"}}
{"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Total Sales"}}
```

Each projection also carries `queryRef` (`Sales.Total Sales`) and `nativeQueryRef` (`Total Sales`). Filters use a `From` alias list with `SourceRef.Source`; the plugin resolves aliases.

## Validation rules you may see

| Rule | Meaning |
|---|---|
| PBIR010-012 | pageOrder, folders, and activePageName disagree |
| PBIR014, PBIR021 | `name` differs from folder name |
| PBIR022-024 | missing position, non-positive size, off canvas |
| PBIR040 | data visual with no title or alt text |
| PBIR050-051 | table or field missing from the model |
| PBIR030 | more than 20 visuals on one page |
