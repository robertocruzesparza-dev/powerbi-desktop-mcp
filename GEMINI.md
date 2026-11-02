# Power BI Desktop (Gemini CLI context)

Follow [AGENTS.md](AGENTS.md): it lists which tool layer to use and the safety rules.

Skills live in `skills/`. Gemini CLI loads them when this folder is installed as an extension
(`gemini extensions install <path or git URL>`), or copy them with
`python scripts/install_skills.py --target gemini`. Start with the `powerbi` skill for any Power BI request.

The MCP server starts through `launch.cmd` on Windows (it sets up a private Python environment the first time,
about 70 seconds). Live analysis of an open file needs Windows and Power BI Desktop; reading and editing saved
`.pbip` projects works without Desktop.
