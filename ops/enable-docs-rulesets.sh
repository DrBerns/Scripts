#!/usr/bin/env bash
# Enable Layer 1 (branch protection) on every repo that has the requirable docs gate.
#
# What this does, per repo, on the default branch (main):
#   - Requires a pull request before merging (blocks direct pushes to main)
#   - Requires the status check "Require CHANGELOG + BACKLOG + SESSION_STATE update" to pass
#   - bypass_actors: []  -> NOBODY bypasses, including admins/owner. This is the part that
#     makes the docs rule actually binding.
#
# Prereqs:
#   - GitHub CLI installed and authenticated as the repo OWNER:  gh auth login
#     (the token needs admin rights on these repos to manage rulesets)
#   - Run in a BASH shell. On Windows that means "Git Bash" (installed with Git for
#     Windows) or WSL -- NOT Command Prompt and NOT PowerShell.
#   - No-install alternative: open a GitHub Codespace on any repo and run it in the
#     Codespace terminal, where `gh` is already installed and authenticated as you.
#
# Safe to re-run: if a ruleset named "docs-gate-required" already exists this will create a
# second one with the same effect; to avoid duplicates, delete the old one first or rename.
#
# Run:  bash enable-docs-rulesets.sh

set -euo pipefail

OWNER="DrBerns"
REPOS=("JFBDC" "CIG" "SpineMed" "PainMed")
CHECK_CONTEXT="Require CHANGELOG + BACKLOG + SESSION_STATE update"
RULESET_NAME="docs-gate-required"

read -r -d '' PAYLOAD <<JSON || true
{
  "name": "${RULESET_NAME}",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [
          { "context": "${CHECK_CONTEXT}" }
        ]
      }
    }
  ],
  "bypass_actors": []
}
JSON

for REPO in "${REPOS[@]}"; do
  echo "=== ${OWNER}/${REPO} -> creating ruleset '${RULESET_NAME}' ==="
  echo "${PAYLOAD}" | gh api -X POST "repos/${OWNER}/${REPO}/rulesets" --input - \
    && echo "OK ${REPO}: ruleset active" \
    || echo "WARN ${REPO}: failed (check gh auth / admin rights / existing ruleset)"
  echo
done

echo "Done. Verify in each repo: Settings -> Rules -> Rulesets (should show '${RULESET_NAME}', Active)."
echo "Test it: open a PR that edits an .html/.js file WITHOUT touching the three docs -- it must be blocked from merging."
