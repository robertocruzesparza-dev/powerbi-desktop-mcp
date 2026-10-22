# Editing: approved, reversible changes

The editing layer changes **copies** of Power BI files, one change at a time, and only after a human approves that exact change. It cannot refresh data, publish, or touch tenant or service settings.

## Turn it on

1. Create a folder for copies inside your profile (not the profile root, not a drive root).
2. Set `PBI_MCP_EDIT_ROOT` to it and restart the MCP client. Unset = editing is off.
3. Put a copy of the file there. Originals elsewhere are refused.

## Supported formats

| Format | What it is | What can be edited |
|---|---|---|
| `.pbip` | Project pointer (JSON) | The file |
| `.pbir` and `.json` inside a `*.Report` folder | Report definition (JSON): pages, visuals, formatting | The file |
| `.tmdl` | Semantic model definition (text) | The file |
| `.pbids` | Data source file (JSON) | The file |
| `.rdl` | Paginated report (XML) | The file. Takes effect only after you re-publish it yourself |
| `.pbit` | Template (zip) | `Report/Layout`, `Report/definition/*.json`, `DataModelSchema`. Experimental |
| `.pbix` | Report (zip) | `Report/Layout`, `Report/definition/*.json` only. Experimental |

For `.pbix`/`.pbit` the compressed model (`DataModel`), Power Query container (`DataMashup`) and every other part are copied byte-for-byte and never opened. To change the model of a `.pbix`, save it as a `.pbip` in Power BI Desktop and edit the `.tmdl` files. Microsoft does not document editing `.pbix`/`.pbit` outside Desktop; open the result in Desktop to confirm it loads.

## The change protocol

1. **Review**: `review_file` reads the exact current text (for `.pbix`/`.pbit`, list parts first).
2. **Propose**: `propose_change(path, old_text, new_text, reason, member?, expected_count?)`. Nothing changes. You get a unified diff, how many places match, lines added/removed and a risk note. `old_text` must match exactly `expected_count` places (default 1).
3. **Wait**: the assistant must show you the diff and wait for your explicit approval of *that* change.
4. **Approve with a code**: the server prints a 6-digit code in **its own console window**, never to the assistant. You read it out. Three wrong tries lock the change; proposals expire after 30 minutes.
5. **Apply**: `finish_change(change_id, "apply", code)`. The file must be unchanged since the proposal. A full-file backup is taken, only the approved edit is written, and the result is re-read and re-validated. If any check fails, the original is restored automatically.
6. **Verify live**: static checks cannot prove a measure evaluates or a visual loads. Reopen the file in Power BI Desktop and run `audit_dashboard`.
7. **Roll back**: `finish_change(change_id, "rollback", code)` with a fresh code restores the backup byte-for-byte. It refuses if the file changed after the edit, so later work is never erased.

Backups and the audit journal live in `<PBI_MCP_EDIT_ROOT>\.pbi-changes\`. The journal records change ids, file names, hashes and timestamps, never file content.

## Built-in safety rules

- Credentials and tokens are never written into files; `review_file` shows them as `[REDACTED]`.
- `.rdl` edits may not contain `DOCTYPE`/`ENTITY` declarations.
- Result validation: JSON must parse; `.rdl` must be well-formed XML; `.tmdl` must not mix tabs and spaces in indentation or leave a code fence open. These are static checks only.
- Symbolic links, the change store, folders and unsupported file types are refused.
- Edits are never batched unless the user approves a batch in words.

## What it cannot do

Refresh schedules, credentials and privacy levels, publishing, tenant settings, and re-publishing `.rdl` files are manual. The assistant should give you the exact steps.
