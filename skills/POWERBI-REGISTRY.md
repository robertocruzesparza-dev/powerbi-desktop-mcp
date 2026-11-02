# Power BI skill registry

The `powerbi` coordinator reads this file, then loads only the specialists a request needs.

| Skill | Use for | Main tools (plugin `powerbi-desktop`, v4) |
|---|---|---|
| `powerbi` | Unclear or broad requests; routing | none |
| `powerbi-full-analysis` | Audit or diagnose an open file or a saved project; ranked findings | `audit_dashboard`, `inspect_project`, `best_practice_scan`, `analyze_project_queries` |
| `powerbi-troubleshooting` | A symptom: blank visual, wrong total, refresh failure, slow, file will not open | symptom table, then the tools it names |
| `powerbi-performance-tuning` | Make a model or report faster or smaller | `analyze_model_size`, `analyze_folding_risk`, `best_practice_scan`, `run_dax_query` |
| `powerbi-dax-authoring` | Write, review, optimize, or explain DAX | `validate_dax_live`, `lint_dax`, `run_dax_query`, `upsert_measure_live` |
| `powerbi-model-design` | Star schema, date table, relationships, RLS, calculation design | `read_model_tmdl`, `plan_model_change` |
| `powerbi-project-authoring` | Create or change a `.pbip` project: model, pages, visuals, themes | `plan_model_change`, `plan_report_change`, `apply_change`, `validate_project` |
| `powerbi-approved-editing` | The approval, backup, verify, rollback rules that every write follows | `apply_change`, `restore_backup`, `*_live` tools |
| `powerbi-file-formats` | What each file type holds and how safely it can be edited | none |
| `powerbi-documentation` | Data dictionary, measure catalog, lineage, handover docs | `document_project`, `diff_projects` |
| `copilot-powerbi-agent` | Packaging the tools for Microsoft 365 Copilot or Copilot Studio | none |

## Layers, from safest to riskiest

1. **Read-only on an open file** (`instance` tools): no change possible.
2. **Read-only on a saved project** (`*_project`, `read_model_tmdl`, `validate_project`): Power BI Desktop not needed.
3. **Planned edits to a project copy** (`plan_*` then `apply_change`): backup, validation gate, automatic rollback.
4. **Live edits to an open file's measures** (`*_live`, dry run first): lands in Desktop's session; the user saves.

Never available by design: refresh, publish, gateway, sharing, tenant settings, credentials, editing the compressed `DataModel` or `DataMashup` inside a `.pbix`.

## Shared rules

- Student and staff data: aggregates only; never copy rows into notes, diffs, or documents.
- Tell the user what could not be checked (`not_checked`, tool `limits`, `caveat`).
- Start every session with `plugin_status` if a tool seems missing; running chats keep the tool list they started with.
