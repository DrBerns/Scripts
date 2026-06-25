# Docs-gate branch protection — setup

This folder enforces **Layer 1** of the docs rule: a branch ruleset on `main` that makes the
CHANGELOG/BACKLOG/SESSION_STATE gate a *required* check, so a change literally cannot merge
without updating the three docs — for anyone, including admins.

Applies to: **JFBDC, CIG, SpineMed, PainMed** (each already has the always-run gate whose
check is named `Require CHANGELOG + BACKLOG + SESSION_STATE update`).

---

## Why this is owner-only

Creating a ruleset needs the repository **Administration** permission. Claude's integration
token does not have it (verified: the API returns `403 Resource not accessible by
integration`), so the ruleset must be created by the repo **owner** — either via the web UI
below or the script (`enable-docs-rulesets.sh`) run as the owner.

---

## Option A — GitHub website (no terminal, recommended)

Do this **once per repo** for JFBDC, CIG, SpineMed, PainMed:

1. Go to `https://github.com/DrBerns/<REPO>/settings/rules`
2. **New ruleset → New branch ruleset**
3. **Ruleset Name:** `docs-gate-required`
4. **Enforcement status:** **Active**
5. **Target branches:** Add target → **Include default branch**
6. ✅ **Require a pull request before merging** (leave required approvals at **0**)
7. ✅ **Require status checks to pass** → **Add checks** → type
   `Require CHANGELOG + BACKLOG + SESSION_STATE update` → select it.
   *(On a repo where the gate hasn't run yet — e.g. PainMed — it may not auto-suggest; type
   the name exactly and add it.)*
8. **Bypass list:** leave **empty** ← this is what makes it bind even on the owner.
9. **Create**

---

## Option B — script (terminal)

`enable-docs-rulesets.sh` does the same thing for all four repos in one run.

- It is a **bash** script. Run it in **Git Bash** (installed with Git for Windows) or WSL —
  **not** Command Prompt and **not** PowerShell.
- It needs the **GitHub CLI** (`gh`) installed and logged in as the owner: `gh auth login`.
- Zero-install alternative: open a **GitHub Codespace** on any repo and run it in the
  Codespace terminal, where `gh` is already installed and authenticated as you.

```bash
bash enable-docs-rulesets.sh
```

---

## Verify it works

On any protected repo, open a PR that edits an `.html` / `.js` / `.py` file **without**
touching the three docs. The merge button must be **blocked** until the docs are added.
That's the proof the rule is real and not advisory.

_Last updated: 2026-06-25_
