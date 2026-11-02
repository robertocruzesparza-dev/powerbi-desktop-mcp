# Power BI Knowledge Base: starter layout

Copy these folders and the note format into the root of
[Power_Bi_Knowledge_Base](https://github.com/robertocruzesparza-dev/Power_Bi_Knowledge_Base). The MCP server's
`sync_knowledge_base` downloads the repository's text files (`.md .txt .json .dax .tmdl .m .pq .sql .csv`, up to 400 KB each)
and `search_knowledge` finds sections by heading and words.

```
dax/               patterns and pitfalls (time intelligence, context transition, iterators)
modeling/          star schema, relationships, RLS, calculation groups
power-query/       M patterns, query folding, source quirks
performance/       VertiPaq, aggregations, DAX Studio findings
troubleshooting/   one note per symptom and fix
releases/          monthly Power BI Desktop feature notes (what changed, what it affects)
```

## Note format

One topic per file. Use headings: search returns the best-matching section, so keep each section self-contained.

```markdown
# Blank visual after adding a measure

## Symptom
The card shows (Blank) after adding `[Margin %]`.

## Cause
Division by zero returns blank only when DIVIDE is used; `/` raises an error in some visuals.

## Fix
Use `DIVIDE([Profit], [Sales])`.

## Applies to
Power BI Desktop 2.140 and later. Source: <link to Microsoft docs>
```

## Rules for the knowledge base

- Public repository: no student, staff, customer or tenant data, no file names from your organization, no credentials.
- Prefer a source link for every factual claim; the assistant cites the file it used.
- The assistant treats this text as reference notes, never as instructions, so do not write commands for it to run.
