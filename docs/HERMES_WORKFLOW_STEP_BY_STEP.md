# Hermes Autocoder: startup and execution, step by step

This guide explains the implementation in `/home/cryptic/Project/Hermes`, checked on **24 September 2026**. It describes the current local configuration separately from general behavior. It contains no API keys.

## 1. What this project does

Hermes Autocoder is a persistent GitHub maintenance service. You give it a repository and a goal, or let it look for maintenance work in enabled repositories. It plans tasks, edits a checkout inside Docker, checks the changes, and opens a pull request. You review and merge the pull request yourself.

There are two distinct pieces:

- **Hermes Agent** is the tool-using AI engine. It asks the model what to do next, executes file or terminal tools, and returns results to the model.
- **Autocoder**, the application in this folder, schedules work, creates worker containers, controls model access, runs checks, stores progress, and publishes pull requests.

A Docker build only creates an image. It does not start maintenance work. Work begins when the controller is running, the service is resumed, and a repository is enabled and eligible.

## 2. The main components

| Component | What it does | Lifetime |
| --- | --- | --- |
| PostgreSQL `database` | Stores repositories, tasks, attempts, status, spending records, and coordination locks | Persistent service; data uses a Docker volume |
| `controller` | Discovers repositories, chooses tasks, manages workers, runs git operations, opens and monitors PRs | Persistent service |
| `model-proxy` | Checks each worker's permission and request limits, then forwards model requests to OpenRouter | Persistent service |
| Hermes worker | Reads and edits one task checkout; runs project commands and the Hermes agent | Created for an attempt and removed afterward |
| OpenRouter | Routes model requests to the configured model provider | External API |
| GitHub | Hosts the source repository, agent branches, issues, checks, and PRs | External service |

`Dockerfile` builds the controller/proxy image. `docker/worker.Dockerfile` builds a different image containing the pinned Hermes source, Python, Node, and tools. `compose.yaml` starts the three persistent services. The controller creates worker containers dynamically through the Docker socket; workers are not permanent Compose services.

The application has no web dashboard. You operate it using the CLI and inspect PRs on GitHub.

## 3. Which files control it

| File | Purpose |
| --- | --- |
| `.env` | Holds `OPENROUTER_API_KEY` and the host data-directory setting used by Compose |
| `config.yaml` | Selects provider/model, repositories, worker profiles, limits, and directories |
| `compose.yaml` | Defines services, networks, mounts, and secrets |
| `secrets/github_personal` | GitHub credential used by the controller |
| `secrets/postgres_password` | Database password used when initializing PostgreSQL |
| `secrets/pgpass` | Database authentication for the application |
| `src/autocoder/controller.py` | Scheduling, planning, implementation, validation, publication, recovery |
| `src/autocoder/hermes_runner.py` | Converts a task into a Hermes Agent session |
| `src/autocoder/proxy.py` | Receives and forwards permitted model requests |
| `src/autocoder/worker.py` | Creates containers and runs commands inside them |

Compose reads `.env` and uses `OPENROUTER_API_KEY` as the source of a Docker secret. Inside the controller and proxy it appears at `/run/secrets/model_provider`. The application reads that file. You do not need to copy the key manually into `secrets/model_provider`.

Workers receive a temporary run token instead of the OpenRouter key. They also do not receive GitHub credentials, database credentials, or the Docker socket. `.env` is excluded from version control and the Docker build context. Avoid sharing expanded Compose configuration because it may contain sensitive values.

## 4. Current local settings

These are the file settings observed when this guide was written:

| Setting | Current value | Meaning |
| --- | --- | --- |
| Pilot repository | `CrypticFate/demohermes` | Initially eligible repository; manual enable/disable choices are also respected |
| Automatic enrollment | Disabled | Other discovered repositories are not automatically enrolled merely because they exist |
| Model | `nvidia/nemotron-3-super-120b-a12b:free` | Tested free OpenRouter model |
| Provider URL | `https://openrouter.ai/api/v1` | Proxy forwards Chat Completions requests here |
| Configured input/output prices | Both zero | Local accounting treats this model as free |
| Data directory | `/home/cryptic/Project/Hermes/data/local` | Persistent workspaces, attempt files, and reports |
| Model requests | 25 per attempt | A cap on requests accepted by the proxy; its one upstream rate-limit retry can add an upstream call |
| Agent iterations | 15 per Hermes session | Caps the agent loop; an iteration is not necessarily one provider request |
| Attempt deadline | 900 seconds | Roughly 15 minutes shared across worker execution steps |
| Attempts | 3 per task | Includes repair attempts; an operator retry resets the allowance |
| Open PR limit | 3 per repository | Limits new work while earlier PRs remain unresolved |
| Worker resources | 2 CPUs, 3 GB RAM | Docker limits |
| Repository discovery interval | 3,600 seconds | Checks repository access roughly hourly when the loop can run |
| Maintenance scan interval | 86,400 seconds | Normal scan eligibility interval; not a promise of exactly daily execution |
| PR polling interval | 5 seconds | Eligibility interval; long task execution can delay polling |
| YAML budget values | $5/day, $20/month | Initialization values; database values may differ after `budget set` |

Use `hc status` to see the active database budgets and task state. Editing budget values in YAML does not replace budgets already stored in the database by the CLI.

The current `demohermes` runtime profile has no dependency-install step. Its independent check only tests whether `index.html` exists. That is a minimal check: it does not prove correct layout, working JavaScript, accessibility, or mobile behavior. Configure stronger checks for stronger automated evidence.

## 5. Start the system

Run these commands from the project directory. They use the existing configuration and credentials. They do not recreate GitHub repositories or erase data.

### Step 5.1: build the application image

```bash
cd /home/cryptic/Project/Hermes
docker compose build controller
```

This packages the current application code. The proxy uses the same image with a different startup command.

The Hermes worker image is already built and pinned in `config.yaml`. If you intentionally rebuild it, use:

```bash
docker build -f docker/worker.Dockerfile -t hermes-autocoder-worker:local .
docker image inspect hermes-autocoder-worker:local --format '{{.Id}}'
```

After rebuilding, update the relevant `profiles.*.image` entries in `config.yaml` to the reported `sha256:...` ID. Changing a tag alone does not change the pinned image used by the controller.

### Step 5.2: initialize while execution is stopped

If work is already running, pause it before this maintenance sequence. On a stopped installation:

```bash
docker compose stop controller model-proxy
docker compose up -d database
docker compose run --rm controller init
docker compose run --rm controller pause
docker compose run --rm controller discover
```

What happens:

1. PostgreSQL starts and becomes healthy.
2. `init` applies database migrations and creates initial application state if missing. New installations start paused. Existing state is preserved.
3. `pause` explicitly keeps execution stopped, including on an existing installation.
4. `discover` calls GitHub and records repositories the configured credential can access. It does not perform coding.

Manual discovery requires the persistent controller to be stopped, because only one controller may hold the database lease.

### Step 5.3: start the persistent services

```bash
docker compose up -d controller model-proxy

hc() {
  docker compose exec controller autocoder -c /app/config.yaml "$@"
}

hc doctor
hc repos list
hc status
```

The `hc` function is a short alias for the CLI inside the running controller. Define it again after opening a new shell.

The controller starts its scheduler loop. The proxy waits for worker model requests. While paused, no new coding task is claimed. Repository discovery and PR polling can still update records.

`doctor` checks configuration, secret availability, budgets, Docker connectivity, the worker network, and pinned images. It does not prove model inference or GitHub coding succeeds.

If no budgets are configured, set positive application ceilings before resuming, for example:

```bash
hc budget set 0.10 1.00
```

This sets $0.10/day and $1/month limits. It does not buy credits. The application requires positive ceilings even when model pricing is zero; OpenRouter quotas are separate.

### Step 5.4: enable the repository and choose work

```bash
hc repos enable CrypticFate/demohermes
```

Enabling is stored in the database. Other repositories already manually enabled can also receive work after resume. Inspect `hc repos list` and disable any you do not want processed:

```bash
hc repos disable OWNER/REPOSITORY
```

For a specific goal, provide a concrete objective and acceptance criteria. For example:

```bash
hc goal add CrypticFate/demohermes \
  "Improve the existing index.html layout for mobile screens" \
  --acceptance "The page has no horizontal overflow at 375px width" \
  --acceptance "Existing content and functionality are preserved"
```

This creates a queued planning task and prints its ID. It does not immediately modify files. Replace the example with your actual task; the current file-existence check cannot independently verify its visual criteria.

Finally:

```bash
hc status
hc resume
```

`resume` permits work. It can activate earlier queued tasks as well as your new goal. Without an explicit goal, enabled repositories can receive automatic maintenance scans.

## 6. How the controller chooses work

The controller performs a scheduling cycle, then waits five seconds before the next cycle. An execution cycle can itself take minutes. The system runs one worker attempt at a time.

Each cycle:

1. Acquires or renews a database lease so another controller does not execute the same work concurrently.
2. When active, reconciles interrupted attempts and pending publication.
3. Discovers repositories if discovery is due.
4. Polls known pull requests if polling is due.
5. When resumed, checks configuration and available budget.
6. Creates eligible maintenance scans.
7. Claims an eligible queued or ready task and executes it.

Task selection checks repository access, whether it is enabled, dependency completion, attempt limits, repository locks, and the open-PR limit. Non-scan work is prioritized over scan tasks.

Discovery excludes archived repositories, excluded forks, empty repositories, repositories without push permission, and configured exclusions. Losing repository access disables it.

Automatic scans look for concrete maintenance work, such as bugs, tests, or documentation corrections. They are instructed not to invent new product features. A scan is skipped if pending work or the open-PR limit prevents it. The controller also avoids repeating analysis when the repository commit and issue context have not changed. Observing a merge makes the repository eligible for another scan sooner.

## 7. Planning: turn a goal into executable tasks

A queued goal or scan enters `planning`.

1. The controller creates an attempt record and prepares a local checkout.
2. It fetches the repository's default branch and records the base commit.
3. It uses a stable branch name, `agent/<task-id>`.
4. It collects open issues/PR context and existing work to help avoid duplicate proposals.
5. It selects the repository's runtime profile and starts a worker container.
6. It issues a temporary model-access token for that attempt.
7. It asks Hermes to inspect project instructions, code, tests, and CI and propose at most five focused tasks.

Each proposal includes an objective, evidence, acceptance criteria, category, and dependencies. Hermes writes a structured plan to `/output/plan.json`. The application validates the structure and rejects invalid dependency graphs. Planning that changes repository files is rejected.

The controller stores valid proposals as separate tasks in `ready`. The original goal becomes `planned`. **`planned` means decomposition finished; it does not mean implementation finished.** A plan can contain no tasks when no justified work is found.

Plan Markdown is rendered for humans. The database remains the authoritative task state. Planning alone does not open a separate plan-only pull request in this execution path.

## 8. How a worker and the model interact

The controller writes a task description to the attempt's `input/context.json`. The worker sees it at `/input/context.json` and launches `autocoder.hermes_runner`.

That adapter creates a Hermes `AIAgent` with the selected model, custom Chat Completions endpoint, file/terminal tools, and run limits. There is no interactive clarification during an unattended run; unresolved questions must be reported as blockers.

A typical exchange is:

1. Hermes sends the objective, conversation, and available tools to the model proxy.
2. The proxy validates the temporary token, task state, model name, request options, budget, and request count.
3. The proxy forwards an authenticated request to OpenRouter using the real API key.
4. OpenRouter returns either text or a tool call, such as `read_file`.
5. Hermes executes the tool inside the worker and adds its result to the conversation.
6. Hermes sends another model request with that result.
7. The loop continues until completion, a limit, or an error.

For example, a model may read `index.html`, edit it, run a check, examine the result, and finally explain its changes. Several model calls can be required for one task. Planning and review also use model calls.

The proxy requests non-streaming upstream responses. If Hermes requests streaming, the proxy converts the completed response into compatible streamed events after accounting for usage.

The worker has a writable checkout at `/workspace`, read-only `.git`, read-only input, and writable output. Its root filesystem is read-only, it runs as a non-root user, and Docker constrains memory, CPU, process count, and temporary storage. Package installation can use outbound networking. The controller performs git commits and pushes outside the worker.

## 9. Implementation: edit and check a task

A ready task enters `running` and receives its own attempt and worker.

1. **Prepare the checkout.** Fetch the current default branch. Preserve the task's stable branch and existing workspace for retries. Merge a changed base where appropriate. An externally changed PR head causes a blocker requiring reconciliation.
2. **Select the profile.** An explicit repository mapping takes priority. Otherwise supported Node/Python layouts can select configured profiles. An unsupported runtime blocks execution.
3. **Install dependencies.** Run profile setup commands. A failed setup command blocks the attempt.
4. **Run baseline checks.** Record the independent check results before implementation. Existing failures become context for Hermes rather than being hidden.
5. **Run Hermes implementation.** Supply the objective, evidence, acceptance criteria, baseline output, and maintainer feedback. Ask for focused changes and meaningful regression tests for behavior changes.
6. **Enter validation.** Record a digest of the resulting source and rerun the configured checks through the controller.
7. **Check changed files.** Reject protected paths, possible secrets, escaping paths, changed symlinks, binary/oversized files, or excessive file counts. No implementation changes is also a blocker.
8. **Run a fresh review session.** Hermes receives the objective, acceptance criteria, diff, and baseline/final results. It returns an acceptance verdict and objections without changing files.
9. **Verify the reviewed source.** Stop the worker and verify that checks/review did not change the source after implementation. Repeat the file gate.

The review is a fresh model session in the same attempt's worker, not a human review or a different independent model. Its requests share the attempt's request/deadline limits with implementation.

A ready-for-review result requires Hermes to report completion, all configured checks to pass, file gates to pass, and the review to accept every acceptance criterion without objections. Passing a weak check provides only weak evidence.

## 10. Publish the result to GitHub

When the publication gates allow it:

1. The controller commits the implementation and records the tested source commit.
2. It writes plan and validation-report Markdown into the checkout.
3. It commits these generated documents separately.
4. It records a pending-publication entry in the database before remote writes.
5. It pushes the task's `agent/...` branch without force-pushing.
6. It creates or updates the pull request and records its number and URL.

The PR includes the objective, plan/report paths, tested commit, and validation summary.

- If completion, checks, and review pass, the PR is ready for human review.
- If ordinary validation is incomplete but publication gates pass, it can publish a **draft PR** and queue bounded repair.
- A protected-file or source-integrity failure blocks publication rather than publishing a draft containing those changes.

The task generally reaches `awaiting_review`. A draft can return to `ready` for another attempt. The service does not merge the PR or deploy the application.

## 11. Human review, CI, and dependent tasks

The controller polls GitHub for merge/closure, authorized maintainer feedback, and failing CI checks.

- A human merge changes the task to `merged`.
- Closing without merge changes it to `closed_unmerged`; it is not silently reopened.
- Maintainer feedback or failing CI can return an awaiting-review task to `ready` for repair, within its attempt allowance.
- A task depending on another task waits until that dependency is **merged**, not merely until it has a PR or passing checks.

This is how a multi-step plan progresses: implement a prerequisite, publish it, wait for the human merge, then implement the dependent change against updated source.

## 12. Read task status correctly

| State | Meaning |
| --- | --- |
| `queued` | Goal or scan waiting for planning |
| `planning` | Planner is inspecting and proposing tasks |
| `planned` | Parent planning task finished; inspect its child tasks |
| `ready` | Executable task waiting for an implementation attempt |
| `running` | Setup, baseline, or implementation is underway |
| `validating` | Final checks and model review are underway |
| `pr_open` | Publication transition after obtaining a PR |
| `awaiting_review` | PR exists; waiting for review, feedback, CI, or merge |
| `merged` | Merge was observed on GitHub |
| `blocked` | A prerequisite, error, or limit prevents progress; read the reason |
| `failed` | Explicit failure state supported by the task model; many execution errors use `blocked` |
| `cancelled` | Operator cancelled the task |
| `closed_unmerged` | PR closed without merging |

One goal can therefore produce multiple task IDs, attempt IDs, and PRs. A task ID identifies the unit of work; an attempt ID identifies one execution of it.

## 13. Failures, quotas, pause, and restart

### Model rate limits

Free inference still has provider quotas and capacity limits. The key check earlier reported a free-tier daily limit of 50, but this is an observed account response, not a permanent guarantee. Gemma returned an upstream shared-pool rate limit while the tested NVIDIA model worked.

For HTTP 429, the proxy can wait and retry once when the supplied retry delay is within its supported bound. Continued rate limits can block the agent attempt. Switching models or adding more tasks does not guarantee capacity.

### Budget and request limits

Each proxy request reserves an estimated cost, then settles using reported token usage and configured prices. Missing usage retains the estimate. Zero prices produce zero local cost, but request-count limits still apply. Daily/monthly budget periods use UTC.

Budget-blocked work has a specific allowance-recovery path. Other blocked tasks generally need investigation and `retry`; the service does not endlessly retry every failure.

### Pause and cancellation

```bash
hc pause
hc cancel TASK_ID
```

Pause prevents new claims and interrupts active work when the controller checks its heartbeat. Cancellation targets a task. These are not guaranteed instantaneous stops. Neither command automatically deletes an already-published branch or PR.

Interrupted work can be requeued for a later resume; cancelled work remains cancelled. At attempt cleanup the controller revokes the temporary model token, removes the worker, releases the repository lease, and saves a report.

### Restart recovery

PostgreSQL and host-mounted data survive ordinary container restarts. The controller can remove abandoned workers, revoke old tokens, preserve workspaces, and requeue interrupted attempts within limits. Pending publication records allow it to reconcile a push/PR operation without automatically rerunning the coding job.

Restart services with:

```bash
docker compose up -d
hc status
```

Paused state is stored in the database. Restarting does not inherently pause or resume an existing installation. If it was active, it may resume execution automatically. Use `hc pause` before intentionally stopping a running deployment when you want it to remain paused afterward.

## 14. Where to find outputs

The current persistent host directory is:

```text
/home/cryptic/Project/Hermes/data/local/
  workspaces/<task-id>/
  runs/<attempt-id>/
    input/context.json
    output/result.json
    output/plan.json       (planning runs)
    output/review.json     (review runs)
    plan.log              (when that mode ran)
    implement.log         (when that mode ran)
    review.log            (when that mode ran)
  report/<task-id>/<attempt-id>.md
```

Generated documents included with implementation PRs use:

```text
plan/<plan-id>/overview.md
plan/<plan-id>/<sequence>-<task-id>.md
report/<task-id>/<attempt-id>.md
```

Reports retain evidence even when publication is blocked. Markdown plan status is a snapshot; use the database-backed CLI for current state.

## 15. Everyday commands

```bash
# Service/container state
docker compose ps

# Task IDs, states, reasons, PR links, and active budgets
hc status
hc repos list

# Follow service logs; Ctrl+C only exits the log viewer
docker compose logs --tail=100 -f controller model-proxy

# Latest available report for a task
hc report show TASK_ID

# Retry after resolving a blocked/failed task's cause
hc retry TASK_ID

# Stop scheduling and interrupt active work at its next check
hc pause

# Restart eligible work
hc resume

# Stop persistent services without deleting stored data
docker compose stop
```

Avoid `docker compose down -v` if you want to retain the PostgreSQL volume. Changing `.env` or `config.yaml` requires the relevant services to be recreated/restarted to load the new settings. Rebuild the application image after source-code changes.

## 16. What has actually been verified

The live OpenRouter test ran the installed Hermes Agent in a disposable container using the key from `key.text` before the key was moved into `.env` configuration. With free NVIDIA Nemotron, Hermes made a `write_file` call, then a `read_file` call, then returned the expected text. All three model responses reported zero cost. See [the test report](../report/OPENROUTER_KEY_TEST_2026-09-24.md).

A separate Compose check verified that the `.env` key was mounted correctly as a secret. Tests also verified free-model accounting and request limits.

Those checks do not establish that the full production database/proxy/GitHub workflow has completed with this new provider. That requires observing an actual goal through planning, implementation, checks, publication, and human review. Use the task reports and PR evidence to determine what a particular run accomplished.

## 17. Source references

- [Compose services](../compose.yaml)
- [Configuration and validation](../src/autocoder/config.py)
- [CLI commands](../src/autocoder/cli.py)
- [Controller lifecycle](../src/autocoder/controller.py)
- [Worker container lifecycle](../src/autocoder/worker.py)
- [Hermes adapter](../src/autocoder/hermes_runner.py)
- [Model proxy](../src/autocoder/proxy.py)
- [Budget and request accounting](../src/autocoder/budget.py)
- [Git preparation and publication gates](../src/autocoder/gitops.py)
- [Task state transitions](../src/autocoder/domain.py)
- [Deployment runbook](DEPLOYMENT.md)
