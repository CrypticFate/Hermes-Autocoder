import html
from datetime import datetime, timezone

from autocoder.redaction import redact

FOOTER = ("Opened by Hermes Autocoder. This PR will not be merged automatically. "
          "Only @{operator}'s comments are used for revisions.")


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if value else "pending"


def escape(value):
    value = html.escape(redact(str(value))).replace("|", "&#124;").replace("`", "&#96;")
    return value.replace("\r", "").replace("\n", "<br>")


def code(value):
    """Inline code for gatekeeper-controlled identifiers (paths, SHAs, branches, model IDs)."""
    return "`" + escape(value).replace("&#96;", "'") + "`"


def command_text(command):
    return command if isinstance(command, str) else " ".join(map(str, command))


def assess(task, checks, review, sensitive):
    attention = []
    failing = [command_text(c["command"]) for c in checks if c["exit_code"] != 0]
    if failing:
        attention.append("Gatekeeper checks failed: " + ", ".join(failing))
    if review is None:
        attention.append("Independent review did not produce a verdict")
    else:
        unmet = [task.acceptance[i] for i, ok in enumerate(review.acceptance_met)
                 if not ok and i < len(task.acceptance)]
        if len(review.acceptance_met) != len(task.acceptance):
            attention.append("Independent review verdicts do not match the plan's criteria")
        if unmet:
            attention.append("Reviewer found unmet criteria: " + "; ".join(unmet))
        if not review.accepted:
            attention.append("Independent review did not accept the change")
        attention += ["Reviewer objection: " + o for o in review.objections]
    if sensitive:
        attention.append("Sensitive changes (tests, CI, build scripts, migrations or dependencies): "
                         + ", ".join(sensitive))
    return not attention, attention


def render_report(task, attempt, result, review, checks, baseline, sensitive, history, model):
    ready, attention = assess(task, checks, review, sensitive)
    lines = [f"# Report: Plan {task.plan_seq:02d}, {escape(task.title)}", "",
             "| Field | Value |", "| --- | --- |",
             f"| Plan | {code(task.plan_path)} @ {code(task.plan_blob_sha or 'unknown')} |",
             f"| Branch | {code(task.branch)} |", f"| Base commit | {code(task.base_sha)} |",
             f"| Model | {code(model)} |", f"| Result | {'Ready' if ready else 'Draft (needs attention)'} |"]
    if attention:
        lines += ["", "## Needs attention", *["- " + escape(s) for s in attention]]
    lines += ["", "## Summary", escape(result.summary) or "No summary provided.", "", "## Acceptance criteria",
              "| # | Criterion | Builder claim | Reviewer verdict | Evidence |", "| --- | --- | --- | --- | --- |"]
    for i, (criterion, met, claim) in enumerate(verdicts(task, result, review), 1):
        evidence = result.criteria[i - 1].evidence if i <= len(result.criteria) else ""
        lines.append(f"| {i} | {escape(criterion)} | {escape(claim)} | {'met' if met else 'not met'} | "
                     f"{escape(evidence)} |")
    lines += ["", "## Checks (run by gatekeeper)", "| Command | Baseline | After | Excerpt |",
              "| --- | --- | --- | --- |"]
    for i, check in enumerate(checks):
        before = baseline[i]["exit_code"] if i < len(baseline) else "not run"
        lines.append(f"| {escape(command_text(check['command']))} | {before} | {check['exit_code']} | "
                     f"{escape(check['output'][-4096:])} |")
    if not checks:
        lines.append("| No checks configured | - | - | - |")
    lines += ["", "## Changes", "| File | Why |", "| --- | --- |"]
    lines += [f"| {escape(f.path)} | {escape(f.why)} |" for f in result.files_changed_rationale]
    if result.tests_added:
        lines += ["", "Tests added: " + ", ".join(escape(t) for t in result.tests_added)]
    lines += ["", "## Services used",
              escape(", ".join(task.services)) + " (ephemeral, per attempt)" if task.services else "None"]
    for title, items in [("Deviations and open questions", result.deviations + result.open_questions),
                         ("Proposed follow-ups (not implemented)", result.follow_ups)]:
        lines += ["", "## " + title, *(["- " + escape(s) for s in items] or ["None."])]
    lines += ["", "## Attempt history", "| # | Kind | Started | Outcome |", "| --- | --- | --- | --- |"]
    lines += [f"| {i} | {escape(a.mode)} | {stamp(a.started_at)} | {escape(a.outcome)} |"
              for i, a in enumerate(history, 1)]
    return "\n".join(lines) + "\n", ready, attention


def verdicts(task, result, review):
    rows = []
    for i, criterion in enumerate(task.acceptance):
        met = bool(review and i < len(review.acceptance_met) and review.acceptance_met[i])
        claim = result.criteria[i].status if result and i < len(result.criteria) else "not reported"
        rows.append((criterion, met, claim))
    return rows


def pr_body(task, result, review, checks, attention, operator):
    lines = [f"Implements **{task.plan_path}**: {escape(task.title)}", "",
             f"Full report: `{task.report_path}`", ""]
    if attention:
        lines += ["### Needs attention", *("- " + escape(s) for s in attention), ""]
    lines.append("### Acceptance criteria")
    for criterion, met, claim in verdicts(task, result, review):
        detail = "met" if met else f"{escape(claim).replace('_', ' ')}: see report"
        lines.append(f"- [{'x' if met else ' '}] {escape(criterion)} ({detail})")
    lines += ["", "### Checks"]
    lines += [f"- `{escape(command_text(c['command'])).replace('&#96;', chr(39))}`: "
              f"{'passed' if c['exit_code'] == 0 else 'failed (exit ' + str(c['exit_code']) + ')'}"
              for c in checks] or ["- No checks configured"]
    lines += ["", "---", FOOTER.format(operator=operator)]
    return redact("\n".join(lines) + "\n")


def plan_pr_body(plans, operator, goal=""):
    lines = ["Proposes implementation plans for operator approval. Merging this PR approves them; "
             "each plan becomes its own implementation PR, in order.", ""]
    if goal:
        lines += ["Goal: " + escape(goal[:500]), ""]
    lines += ["### Plans", *(f"- `{p.path}`: {escape(p.title)} ({len(p.acceptance_criteria)} criteria)"
                            for p in plans)]
    lines += ["", "---", FOOTER.format(operator=operator)]
    return redact("\n".join(lines) + "\n")
