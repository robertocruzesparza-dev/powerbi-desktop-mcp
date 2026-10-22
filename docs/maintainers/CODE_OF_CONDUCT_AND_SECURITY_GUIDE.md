# Guide: CODE_OF_CONDUCT.md and SECURITY.md

Not legal advice. This is what experienced open-source maintainers and security teams expect to see, and what to do before you publish.

## CODE_OF_CONDUCT.md

### What this draft is
The unmodified **Contributor Covenant 2.1** (CC BY 4.0), the most widely used code of conduct on GitHub, with its attribution section kept. Using a standard text avoids inventing rules, gives contributors something familiar, and lets GitHub recognise it as a community standard.

### The one thing you must do
Replace `[INSERT CONTACT METHOD]` (under *Enforcement*) with a private way to reach the enforcers. Then delete the HTML comment at the top.

- Best: a dedicated project address (`conduct@...` or a mailbox only you read). Do not use a work address (an employer can read it and you may leave) and think twice about a personal address you do not want published.
- Acceptable: a GitHub-based private channel, if you have an organization that supports it.
- Never leave the placeholder: a code of conduct with no working contact is treated as unenforced.

### What a professional checks
1. **There is a real, private reporting route**, and it is monitored. Test it by sending yourself a message.
2. **Enforcement is possible.** With one maintainer, who reviews a complaint about *you*? Name a backup (a trusted co-maintainer or the GitHub organization's owner) once you have one, and say in the file what happens if the report concerns the maintainer.
3. **The scope matches reality.** The Covenant covers project spaces (issues, PRs, discussions) and public representation of the project. Keep it that way; do not try to police people's private lives.
4. **Confidentiality.** Reports and reporters are handled privately, and you can say so in the contact section. The Covenant already says enforcers must respect the privacy of the reporter.
5. **You apply it consistently and proportionately,** using its four-step ladder (correction, warning, temporary ban, permanent ban). Keep short private notes of decisions (date, what happened, action), not a public shaming log.
6. **It is linked.** `README.md` or `CONTRIBUTING.md` should point to it (CONTRIBUTING can say: "Participation is governed by our Code of Conduct"). GitHub shows it in the Community Standards checklist once the file exists at the root or in `.github/`.
7. **Attribution stays.** CC BY 4.0 requires it. Customisations are allowed but make them additions, not edits to the standards, and say so.

### Optional additions some projects make
- A line that bug reports must not include private data (already in `CONTRIBUTING.md`).
- A statement on AI-generated contributions (disclosure expected, contributor remains responsible).
- A short note on languages/accessibility of communication.

## SECURITY.md

### What this draft contains and why
| Section | Why it is there |
|---|---|
| Private reporting route | GitHub shows *Security policy* only when the file exists; the first rule is to keep reports off public issues. The fallback ("Security contact request") means a reporter without GitHub private reporting can still reach you without disclosing details |
| What to include / what not to include | Saves back-and-forth, and stops reporters from sending real student or employee data |
| Response targets | Reporters judge a project by whether it acknowledges them. Targets are stated as targets, not guarantees |
| Disclosure timeline (90 days) | Industry norm (Google Project Zero and many CNAs use 90); it gives you time and gives reporters certainty |
| Supported versions | Says which versions get fixes, so users know to upgrade |
| Scope in / out | Prevents invalid reports (vendor bugs, deliberately unsafe flags) and shows what you consider part of the trust boundary |
| Severity examples | Aligns expectations; tied to this project's actual risks |
| Safe harbor | Researchers need to know good-faith testing will not get them in trouble. Kept narrow: you can only speak for your own code |
| Design controls table | Lets reviewers check your claims against the code, and users understand the limits |
| Known limits / using it safely | Honest about what the approval code does not protect against; operational advice for users |

### Before you publish: make every sentence true
A security policy is a promise. Check each item:

- [ ] **Enable GitHub private vulnerability reporting** (Settings > Security > Code security > Private vulnerability reporting). Otherwise the first instruction in the file does not work.
- [ ] You can meet the **7-day acknowledgement**. If not, change it to a number you can keep. A missed promise looks worse than a longer one.
- [ ] You are willing to **publish an advisory** and request a CVE through GitHub when a report is confirmed.
- [ ] The control table matches the code: re-read `pbi_mcp/editing/` and `pbi_mcp/http_auth.py` and fix the table if you change a number (tries, lifetimes).
- [ ] "Releases are not currently signed": update this when you adopt PyPI Trusted Publishing, GitHub artifact attestations or Sigstore signing.
- [ ] If you are not comfortable with the **safe-harbor** paragraph, delete that section rather than soften it halfway. Do not promise protection from third parties.
- [ ] The **supported versions** table is updated at each release.

### What a security professional would still look for (not in the file)
1. **Working private channel tested end-to-end** (submit a test advisory from a second account).
2. **Secret scanning and push protection on**, Dependabot alerts on (`.github/dependabot.yml` already exists), `pip-audit` or `osv-scanner` in CI.
3. **Threat model kept current.** The tunnel being anonymous is the weakest link; the README and SECURITY say so. Keep that visible.
4. **Disclosure drill.** Decide now who you tell, how you patch quietly (private fork or GitHub's temporary private fork), and how you notify users (release notes, advisory, README banner).
5. **Reproducible, verifiable releases.** Build from a tagged commit in CI; publish with Trusted Publishing; consider attestations and a `SHA256SUMS` file. Then say so in the policy.
6. **A `security.txt`** is for websites; for a GitHub project the `SECURITY.md` is the standard.
7. **Data-handling transparency.** `PRIVACY.md` and the security policy must agree (what is logged, where backups live, who sees query results).
8. **Dependency pinning vs. freshness.** Ranges are used for compatibility; run CI against the newest allowed versions so you notice breakage early.

### Mistakes to avoid
- Putting a personal email that you will not monitor.
- Promising a bounty, SLA or legal protection you cannot provide.
- Listing unfixed issues publicly in detail before a fix exists.
- Treating the approval code as a security boundary against malware: the policy states the real limit.
- Forgetting to credit reporters (they talk to each other).

## Quick order of operations
1. Decide the contact method; fill the Code of Conduct placeholder.
2. Enable private vulnerability reporting, secret scanning and Dependabot.
3. Re-read both files once, out loud, asking "can I keep every promise?"
4. Push; in GitHub open *Insights > Community Standards* and confirm the checklist is green.
5. Send yourself a test report through each channel.
