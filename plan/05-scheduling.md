# Phase 5: Durable Scheduling and Budgets

Implement global/repository leases, persisted attempts, heartbeats, interrupted
run recovery and a publication outbox. Enforce explicit budgets with atomic
reservations and conservative accounting when provider outcomes are unknown.

Acceptance: concurrent budget requests cannot overspend the configured estimate;
cancelled, expired or revoked capabilities cannot make calls; restart preserves
work; dependent work cannot run before merge. One worker globally in v1.
