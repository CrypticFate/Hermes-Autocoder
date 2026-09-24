# Phase 7: Pull Requests

Publish only agent branches, never force-push or merge. Reconcile a stable PR
identity, poll CI and authorized maintainer feedback, and append repair commits.
Only observed GitHub merge events unlock dependencies.

Acceptance: no duplicate PR after timeout/restart; closed unmerged PRs remain
distinct; external branch edits block reconciliation; human merge updates state.
