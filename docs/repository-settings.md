# Repository settings

The built-in deployment supports one control-plane process and one journal
writer. Repository merges should preserve the release gates rather than bypass
them.

For `main`, require these successful checks before merging:

- `check`
- `images`
- `kind-e2e`

Also require a pull request (or an equivalent controlled merge), dismiss stale
approvals when the head changes, and prohibit force-pushes to release tags.
Future release tags should be annotated and, where the organization supports
it, signed. The v1.0.1 tag is historical and must not be recreated or moved.

An administrator can apply the branch policy with the GitHub CLI after
reviewing organization-specific settings:

```bash
gh api --method PUT repos/negativexq/agentic-sre/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["check", "images", "kind-e2e"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "required_approving_review_count": 1,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

This policy protects code and CI gates; it does not provide API identity,
multi-user authorization, or multi-replica journal-writer safety.
