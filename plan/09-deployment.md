# Phase 9: VPS Deployment

Supply Compose, controller/worker Dockerfiles, secret-file configuration, health
checks, persistent volumes, bounded logs, backup tooling and restore/rollback
instructions. Keep internal ports private. Pin tested deployment image digests.

Acceptance: clean VPS startup, reboot recovery, provider outage and token
expiration handling, disk-pressure stop, and restore into an empty environment.
These operational checks require a target VPS and are not inferred from unit tests.
