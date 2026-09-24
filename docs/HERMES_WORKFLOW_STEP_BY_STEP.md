# Hermes Autocoder v2: Step by Step

A walk-through of one plan from file to merge. Details: [ARCHITECTURE.md](ARCHITECTURE.md).

1. **You write** `plans/01-hello.md` on `main` (or say "add a plan …" in chat, which opens a plan PR you merge).
2. **Intake.** On its next tick (15 s) the gatekeeper fetches `main`, reads the committed plan blob, validates
   it, and creates task `pending`, plan 01.
3. **Claim.** No other task of the repository is active and no lower plan is unfinished, so the task moves
   `pending → queued → preparing` under the global lease. Branch: `agent/01-hello`.
4. **Protection check.** The gatekeeper confirms the ruleset still requires your approval and the bot still has
   exactly Write access. If not, the repository is blocked and you are notified; nothing runs.
5. **Sandbox.** It clones the branch, creates the internal network `att-<attempt>`, starts any plan services, and
   starts the builder: read-only root, non-root, no capabilities, `.git` read-only, no Docker socket, no
   credentials.
6. **Setup.** Setup commands run with the temporary `hermes-setup-egress` network, which is then detached; a
   probe proves github.com and openrouter.ai are unreachable before anything else runs.
7. **Baseline.** Your checks run once before any change, so the report can show what was already failing.
8. **Build.** A capability token is issued; Hermes (memory off, terminal and file tools only) implements the plan
   and writes `result.json`. The token is revoked when it exits.
9. **Gates.** Changed files may not touch `plans/`, `report/`, `.git`, submodules or git attribute drivers, and
   may not contain secrets, symlinks, binaries or huge files. The builder's criteria must match the plan 1:1.
10. **Verify.** Checks run again in a fresh read-only container with no token or egress. A fresh Hermes session
    reviews the diff against each criterion. A digest proves neither altered the code.
11. **Report.** Commit `plan 01: Hello`, render `report/01-hello.md` from the database and the measured
    results, commit `report 01: Hello`.
12. **Publish.** Protection is re-verified once more, HEAD must equal the validated commit, and only
    `refs/heads/agent/01-hello` is pushed. PR `[Plan 01] Hello` opens (draft if anything needs attention),
    labeled `autocoder`. You get a notification.
13. **Review.** Comment or request changes; after 3 quiet minutes a repair adds commits to the same PR.
    Comments from anyone else are ignored and reported to you.
14. **Merge.** You approve and merge on GitHub. The gatekeeper marks the task merged, deletes the branch,
    rescans plans, and plan 02 becomes eligible.
