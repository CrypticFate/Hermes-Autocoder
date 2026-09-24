import html

from autocoder.redaction import redact


def escape(value):
    value = html.escape(redact(str(value))).replace("|", "&#124;").replace("`", "&#96;")
    return value.replace("\r", "").replace("\n", "<br>")


def render_report(task, attempt, result, review, checks, baseline, sensitive, history, model):
    ready = (all(c["exit_code"] == 0 for c in checks) and review is not None and review.accepted
             and not review.objections and len(review.acceptance_met) == len(task.acceptance)
             and all(review.acceptance_met) and not sensitive)
    attention = []
    if not all(c["exit_code"] == 0 for c in checks):
        attention.append("Gatekeeper checks failed")
    if review is None or not review.accepted or review.objections or not all(review.acceptance_met):
        attention.append("Independent review has unmet criteria or objections")
    if sensitive:
        attention.append("Sensitive changes: " + ", ".join(sensitive))
    lines = [f"# Report: Plan {task.plan_seq:02d}, {escape(task.title)}", "",
             "| Field | Value |", "| --- | --- |",
             f"| Plan | {escape(task.plan_path)} @ {escape(task.plan_blob_sha)} |",
             f"| Branch | {escape(task.branch)} |", f"| Base commit | {escape(task.base_sha)} |",
             f"| Model | {escape(model)} |", f"| Result | {'Ready' if ready else 'Draft (needs attention)'} |"]
    if attention:
        lines += ["", "## Needs attention", *["- " + escape(s) for s in attention]]
    lines += ["", "## Summary", escape(result.summary), "", "## Acceptance criteria",
              "| # | Criterion | Builder claim | Reviewer verdict | Evidence |", "| --- | --- | --- | --- | --- |"]
    for i, criterion in enumerate(result.criteria):
        verdict = "met" if review and i < len(review.acceptance_met) and review.acceptance_met[i] else "not met"
        lines.append(f"| {i+1} | {escape(criterion.criterion)} | {criterion.status} | {verdict} | {escape(criterion.evidence)} |")
    lines += ["", "## Checks (run by gatekeeper)", "| Command | Baseline | After | Excerpt |", "| --- | --- | --- | --- |"]
    for i, check in enumerate(checks):
        before = baseline[i]["exit_code"] if i < len(baseline) else "not run"
        lines.append(f"| {escape(check['command'])} | {before} | {check['exit_code']} | {escape(check['output'][-4096:])} |")
    lines += ["", "## Changes", "| File | Why |", "| --- | --- |"]
    lines += [f"| {escape(f.path)} | {escape(f.why)} |" for f in result.files_changed_rationale]
    lines += ["", "## Services used", escape(", ".join(task.services) or "None")]
    for title, items in [("Deviations and open questions", result.deviations + result.open_questions),
                         ("Proposed follow-ups (not implemented)", result.follow_ups)]:
        lines += ["", "## " + title, *["- " + escape(s) for s in items]]
    lines += ["", "## Attempt history", "| # | Kind | Started | Outcome |", "| --- | --- | --- | --- |"]
    lines += [f"| {i} | {escape(a.mode)} | {a.started_at} | {escape(a.outcome)} |"
              for i, a in enumerate(history, 1)]
    return "\n".join(lines) + "\n", ready, attention


def pr_body(task, checks, attention, operator):
    return redact(f"Implements **{task.plan_path}**: {task.title}\n\nFull report: `{task.report_path}`\n\n"
                  + ("### Needs attention\n" + "\n".join("- " + escape(s) for s in attention) + "\n\n" if attention else "")
                  + "### Checks\n" + "\n".join(f"- {escape(c['command'])}: exit {c['exit_code']}" for c in checks)
                  + f"\n\nOnly @{operator}'s comments are used for revisions. Human review and merge required.")
