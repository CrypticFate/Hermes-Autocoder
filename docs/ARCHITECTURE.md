# Architecture and Contracts

## Data Flow

The controller lists accessible repositories for configured resource owners.
Enabled repositories enter a maintenance scan at most once a day. User goals
take priority. A planner returns a validated dependency graph with evidence and
acceptance criteria. Each task gets a stable branch and isolated checkout.

The controller prepares dependencies in the sandbox, runs a baseline, invokes
Hermes, independently reruns configured checks, and asks a fresh Hermes session
to review the diff and acceptance criteria. A passing result creates a ready PR;
incomplete local validation creates a draft and triggers bounded repair. Sensitive
paths, possible secrets, unsupported runtimes and infrastructure errors block
publication. All attempts retain a local report.

The controller observes human merges and unlocks dependent tasks. It polls
authorized maintainer comments and CI outcomes to repair existing PRs. There is
no merge or deployment endpoint in the controller.

## Interfaces

- `TaskContext` is a versioned, validated request containing stable IDs, mode,
  objective prompt, base commit, model, iteration and duration limits.
- `AgentRunner.run(TaskContext) -> RunResult` is the engine adapter. `RunResult`
  is untrusted evidence, not authorization to publish or mark a task merged.
- `PlanResult` contains up to five `Proposal` records with unique local keys,
  objectives, evidence, acceptance criteria, categories and dependencies.
  Validation rejects missing dependencies and cycles before persistence.
- `ReviewResult` records the independent verdict, one acceptance result per
  criterion, and concrete objections.
- The model proxy exposes only `POST /v1/chat/completions` and `GET /health`.
  It accepts text/tool requests for the configured model. Provider completions
  are buffered and accounted before returning JSON or Hermes-compatible SSE.
  Images, files, alternate models and provider overrides are unsupported.
- CLI configuration is versioned YAML. Runtime profiles supply pinned images,
  setup commands and independently selected check commands as argument arrays.

## Persistence and States

PostgreSQL is authoritative. Markdown is a human-readable projection.

`queued -> planning -> planned` handles goals and maintenance scans. The validated
plan creates executable tasks in `ready`. Execution follows
`ready -> running -> validating -> pr_open -> awaiting_review -> merged`.
`blocked`, `failed`, `cancelled`, and `closed_unmerged` preserve distinct outcomes.
Operator retry requeues blocked work. Closed PRs are not silently reopened.

A global database lease permits one active controller. Repository leases prevent
overlapping writes. A restart first reconciles publication records, then stops
abandoned workers and requeues interrupted work within its attempt allowance.
GitHub branch and PR identity are stable across retries. Push never force-updates
a branch. A publication record is written before contacting GitHub so a timeout
after a successful write can be reconciled without repeating the coding job.

## Budget Accounting

The proxy hashes temporary run credentials in the database. Tokens expire at the
task deadline and are revoked when the attempt finishes. Every model request
locks the control row, checks task state and request count, and reserves a
conservative text-token estimate against UTC daily and calendar-month ceilings.
Provider usage settles the reservation; missing usage or unknown outcomes retain
the full reservation. Retries, planning and independent review all count.

Configured prices must cover the provider's actual rates, including reasoning
tokens charged as completion tokens. Unexpected usage above reservation pauses
the service. This is an application spending guard, not a promise about the
provider's eventual bill. Configure corresponding account-side limits.

## Isolation and Limits

Workers are non-root, use a read-only root filesystem, dropped capabilities,
no-new-privileges, bounded CPU/memory/PIDs/temp space and a task deadline.
Workspace usage and free disk are checked during execution. This polling guard
is not a filesystem quota; put the data directory on a dedicated quota-limited
filesystem on a shared VPS. Public outbound network access supports package
installation. Host services must not expose credentials on worker-accessible
network interfaces. This is not a sandbox for deliberately hostile public code.

Worker images are built from a fixed Hermes source commit. Operator runtime
profiles must use a tested image ID/digest. Project Python/Node versions may
require additional image profiles; the default image contains Python 3.13 and
Node 22. Unsupported or missing checks block a task rather than claim success.

## Deliberate v1 Boundaries

- One worker at a time; no web dashboard or automatic merging.
- No fork contributions or cross-repository dependency graphs by default.
- CI workflow edits and destructive migration paths require a separately
  authorized human workflow; there is no approval-bypass switch.
- Test success plus model review is useful evidence, not proof of correctness.
  Human PR review remains part of the completion workflow.
- Private package registries and test databases need operator-provided runtime
  profiles and isolated infrastructure; production secrets are never forwarded.
