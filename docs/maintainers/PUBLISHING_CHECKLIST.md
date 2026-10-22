# Publishing checklist (maintainers)

Work top to bottom. Items marked **DECIDE** are yours; nobody can make them for you. Delete this file or keep it as maintainer documentation.

## 1. Decisions before anything goes public

- [ ] **DECIDE: license.** `LICENSE` is MIT with the holder "Roberto Cruz Esparza", year 2026. Confirm the name, or choose Apache-2.0 (adds an explicit patent grant). If you wrote this while employed, check your employer's or district's intellectual-property policy and get written permission to open-source it.
- [ ] **DECIDE: name and owner.** Docs assume `github.com/robertocruzesparza-dev/powerbi-desktop-mcp` (taken from your other repo's remote; the repo does not exist yet). Search GitHub and PyPI for the name first (`pip index versions powerbi-desktop-mcp`; PyPI availability is not verified). Microsoft publishes its own Power BI MCP servers: make the README say how yours differs (analysis breadth + approved edits of copies).
- [ ] **DECIDE: org or personal account.** An organization account (or a separate account) keeps the project's issues and security reports off your personal profile.
- [ ] **DECIDE: scope of the first release.** Suggested: tag `v3.2.0` as *beta* (Development Status in `pyproject.toml` is 4 - Beta). The live tools were validated on one real tenant; `.pbix`/`.pbit` editing is experimental.
- [ ] **Read PRIVACY.md and SECURITY.md** and make sure every statement is true for your build. Replace nothing silently: they are legal-ish promises.

## 2. Remove anything private (a test already blocks the obvious)

- [ ] `python -m unittest discover -s copilot/tests` passes (it fails on organization names, your profile path, tenant GUID fragments and real tunnel hosts).
- [ ] Search by hand for what a test cannot know: colleague names, school/site names, real measure or table names from real dashboards, screenshots, ticket numbers.
- [ ] Never publish the files in your private working folder: the IT request letters (they contain a tenant ID and an email), built `.plugin` or `.zip` packages, `Power BI Snapshots`, backups, `launcher.log`.
- [ ] If anything private was ever committed to this repo, do **not** just delete it in a later commit: history keeps it. Start from a fresh `git init` (as the first commit here does) or rewrite history, and rotate any credential that leaked.
- [ ] Run a secret scanner on the repo: `gitleaks detect` or `trufflehog git file://.`; enable GitHub secret scanning and push protection.
- [ ] Confirm every image/asset: `copilot/appPackage/color.png` and `outline.png` are yours to license. Confirm you wrote (or may license) every bundled skill in `skills/`: five skills, four of them copied from your separate Master_Skills library, whose provenance I could not verify.

## 3. Legal and trademark

- [ ] Keep the "not affiliated with Microsoft" notice. Do not use Microsoft or Power BI logos, and avoid names that imply endorsement ("Official", "Microsoft ..."). Check Microsoft's brand guidelines if you want to use the Power BI name in the repo title.
- [ ] Re-verify dependency licenses (`THIRD-PARTY-NOTICES.md`) for each release.
- [ ] No Microsoft binaries are redistributed (ADOMD.NET comes from the user's Power BI Desktop; Python is downloaded from python.org at run time). Keep it that way.
- [ ] If you ever add telemetry, it must be opt-in and documented in `PRIVACY.md`.

## 4. Repository setup (GitHub)

- [ ] Create the repo (no README/license from the web UI: you already have them), then `git remote add origin ...` and push `main`.
- [ ] Settings > General: description, topics (`mcp`, `model-context-protocol`, `power-bi`, `dax`, `tmdl`, `claude`, `copilot`), disable Wiki if unused.
- [ ] Settings > Security: enable **private vulnerability reporting**, Dependabot alerts, secret scanning + push protection.
- [ ] Settings > Branches: protect `main` (require the CI workflow to pass, require PR review if you have collaborators).
- [ ] Add **CODE_OF_CONDUCT.md**: on GitHub use *Insights > Community Standards > Add* and pick the Contributor Covenant, then put a real contact method in it. (Not generated here so the wording and contact are yours.)
- [ ] Optional: `CODEOWNERS`, `SUPPORT.md` (where to ask questions: Discussions vs issues), `FUNDING.yml`.
- [ ] Check the CI run is green on a fresh clone. CI uses Python 3.10 and 3.12 on Windows; Python 3.13 is not covered (pythonnet support there is unverified).

## 5. Verify a clean install on another PC before announcing

On a PC that has never seen your files (a spare machine, a VM or a colleague), with Power BI Desktop installed:

- [ ] `pip install git+https://github.com/<owner>/powerbi-desktop-mcp` then `powerbi-desktop-mcp --doctor` is all OK.
- [ ] Register it in Claude Desktop / Claude Code / VS Code; open a sample `.pbix`; run `audit_dashboard`.
- [ ] Plugin route: clone, add as local plugin, first start finishes (about 70 seconds), later starts are fast. **Not verified for every app version: the plugin may need a `marketplace.json` wrapper to install from a folder or GitHub URL.** Test it; add the file if required.
- [ ] Editing: set `PBI_MCP_EDIT_ROOT`, copy a sample file there, run propose, approve with the on-screen code, reopen in Power BI Desktop, run an audit, roll back. Do this for a `.pbip`/`.tmdl` project and once for a `.pbix` Report/Layout change (experimental).
- [ ] Copilot build: only claim support after a real end-to-end run in a tenant you control (upload, sign-in, tool call, confirmation prompt). The README currently says what was and was not verified; keep it honest.
- [ ] A PC with no Python, a locked-down PowerShell policy, and behind a proxy (all known gaps).

## 6. Release

- [ ] Update `CHANGELOG.md`; bump `pbi_mcp/__version__`, `.claude-plugin/plugin.json` and `copilot/appPackage/manifest.json` together (a test enforces they match).
- [ ] `python -m pip install build twine && python -m build && twine check dist/*`.
- [ ] Upload to **TestPyPI** first (`twine upload -r testpypi dist/*`), install from it in a clean venv, then PyPI. Use PyPI **Trusted Publishing** from a GitHub Actions workflow instead of storing a token; never commit tokens.
- [ ] Tag `v3.2.0`, create a GitHub Release with the changelog entry and the wheel/sdist attached.
- [ ] Do not attach built Copilot packages that embed your tunnel URL or auth config ID.
- [ ] Optional: submit to MCP directories/registries (check each one's current requirements; not verified here), post in the Power BI community, write a short blog post with a screen recording (mask the data).

## 7. After publishing

- [ ] Watch issues for the first weeks; reply within a few days or say in the README that support is best-effort.
- [ ] Keep a "Known limits" list current (`docs/TOOLS.md`): Power BI Desktop changes monthly (engine metadata functions, file formats, PBIR). Re-test against the current Desktop release every month or two, and note the tested version in the README.
- [ ] Add a short "What I tested on" table (Desktop version, Windows version, client versions).
- [ ] Decide how long you will maintain it, and say so (README banner or `SUPPORT.md`).
- [ ] If you ever publish the Microsoft 365 Copilot app, you also need real privacy and terms URLs (already pointed at the repo's `PRIVACY.md` and `LICENSE`), an owner for the Entra app, and a plan for tenant admin review.

## Suggested files still to draft

| File | Why |
|---|---|
| `CODE_OF_CONDUCT.md` | Expected by GitHub community standards; pick Contributor Covenant and add a contact |
| `SUPPORT.md` | Where questions go and what response time to expect |
| `docs/ARCHITECTURE.md` | One page: how the server finds Desktop's engine, why the Copilot build needs a tunnel, how the edit store works. Helps contributors |
| `docs/TESTED-WITH.md` | Versions you actually tested; update per release |
| `examples/` | A synthetic sample `.pbip`/`.tmdl` project and a transcript of one propose/approve/rollback session (no real data) |
| `.github/CODEOWNERS` | Auto-request review |
| A `marketplace.json` | Only if the Claude app needs it to install the plugin from the repo |
| Short demo GIF | Biggest single boost to adoption; mask all data |
