# Phase 3: Workers

Use disposable non-root Docker workers with a pinned Hermes source/image,
read-only root and git metadata, bounded resources and task-scoped model tokens.
Provide Node/Python profiles and explicit overrides for other runtimes.

Acceptance: worker startup/import test, no GitHub/host credentials, no Docker
socket, workspace isolation, command timeout and cancellation. Unsupported
profiles block with a report. VPS filesystem quotas are an operator prerequisite.
