> **Historical (v1).** These are the v1 build notes for this service, kept for reference. The v2
> specification is `HERMES_AUTOCODER_V2_IMPLEMENTATION_PLAN.md`; progress is in
> `docs/IMPLEMENTATION_LOG.md`. (Managed repositories use `plans/`, plural; see the operator guide.)

# Implementation Index

| Phase | Specification | Local implementation | Live acceptance |
| --- | --- | --- | --- |
| 1 | [Foundation](01-foundation.md) | Implemented | Automated checks |
| 2 | [GitHub discovery](02-github.md) | Implemented | Requires account configuration |
| 3 | [Workers](03-workers.md) | Image built; actual Hermes tool loop tested | Target repository runtimes need pilot verification |
| 4 | [Planning](04-planning.md) | Implemented | Requires configured model |
| 5 | [Scheduling and budgets](05-scheduling.md) | Implemented | Requires VPS interruption tests |
| 6 | [Coding and validation](06-coding.md) | Implemented | Requires configured model |
| 7 | [Pull requests](07-pull-requests.md) | Implemented | Requires real test repository |
| 8 | [Reports and CLI](08-operations.md) | Implemented | CLI/fixture checks |
| 9 | [VPS deployment](09-deployment.md) | Images built; Compose and backup timer provided | Target VPS not configured |
| 10 | [Pilot](10-pilot.md) | Acceptance procedure provided | 48-hour live pilot pending |

Runtime task plans use separate ID-named subdirectories. This index describes
implementation of the service itself, not completion of live repository work.
