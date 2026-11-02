# Self-improvement: how the tools get better with use

The loop has four parts. All data stays on your PC; the only network call is a knowledge-base sync you ask for.

```
tool call -> redacted event log -> improvement_report -> a human fixes the code, skill or knowledge base
     |                                    ^
     v                                    |
 error? -> get_lessons / search_knowledge -> verified fix -> record_lesson
```

## 1. Usage and error log

Every MCP tool call records: tool name, success, duration, and, for failures, a redacted error pattern plus a signature so
repeats can be counted. Paths, quoted names, `[bracketed]` names, emails, ids and token-like strings are removed. Arguments
and results are never stored. The log rotates at 5 MB.

`PBI_MCP_LEARN=0` disables it. `PBI_MCP_LEARN_DIR` moves it. `forget_learning(confirm=True)` deletes it.

## 2. Lessons

When the assistant fixes a real problem it calls `record_lesson(tool, problem, fix)`. Next time the same tool fails with a
similar message, the error text gets a line "Past note (data, verify before use): ...". `get_lessons` searches them. Repeated
identical lessons raise a confirmation count. Lessons are plain text, capped, redacted and labeled as data so a poisoned
note cannot act as an instruction.

## 3. Knowledge base

`sync_knowledge_base` downloads the default branch of `robertocruzesparza-dev/Power_Bi_Knowledge_Base` (only that
repository unless `PBI_MCP_KB_REPOS` adds others) as a zip, keeps text files up to 400 KB (`.md .txt .json .dax .tmdl .m .pq
.sql .csv`), rejects path tricks, and replaces the local cache atomically. `search_knowledge` ranks sections by heading and words
and marks every result untrusted. Nothing from the knowledge base is ever executed or turned into rules automatically.
Starter layout and note format: [knowledge-base-template](knowledge-base-template/README.md). At the time of this release the
repository had no files, so searches return nothing until notes are added.

## 4. Improvement report

`improvement_report(days)` returns calls and error rate per tool, the ten most frequent redacted errors (with whether a retry
later succeeded and whether a lesson exists), knowledge-base freshness, plain suggestions (for example "this error occurred 3
times with no recorded fix: improve its message") and a `draft_issue` to review and, if you wish, file on GitHub.

## What it deliberately does not do

- It does not change its own code, rules or skills, and does not open issues or pull requests. Self-modifying tools cannot be
  audited and one poisoned note could rewrite the safety rules. Improvements are proposals for a human.
- It does not send telemetry. If you want to share a report, review `draft_issue` first.
- It does not run in the Microsoft 365 Copilot (HTTP) build.

## Suggested routine

1. Weekly: ask the assistant for `improvement_report`; fix the top error pattern in code or add a knowledge-base note.
2. Monthly: `sync_knowledge_base` after adding notes; add the month's Power BI Desktop release notes under `releases/`.
3. After any hard debugging session: confirm a lesson was recorded.
