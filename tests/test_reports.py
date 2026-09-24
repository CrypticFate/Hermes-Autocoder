from pathlib import Path
from types import SimpleNamespace

from autocoder.contracts import ReviewResult, RunResult
from autocoder.reports import pr_body, render_report

SNAPSHOT = Path(__file__).parent / "snapshots" / "report_01.md"


def fixture():
    task = SimpleNamespace(plan_seq=1, title="One", plan_path="plans/01-one.md", plan_blob_sha="blob",
        branch="agent/01-one", base_sha="base", acceptance=["Works", "Documented"], services=["postgres"],
        report_path="report/01-one.md")
    result = RunResult(summary="All tests pass\n# Forged heading<script>", criteria=[
        {"criterion": "Works", "status": "met", "evidence": "a|b"},
        {"criterion": "Documented", "status": "partial", "evidence": "README"}],
        files_changed_rationale=[{"path": "app.py", "why": "implements it"}],
        deviations=["none really"], follow_ups=["add caching"])
    checks = [{"command": "pytest", "exit_code": 1, "output": "AssertionError"}]
    baseline = [{"command": "pytest", "exit_code": 1, "output": ""}]
    history = [SimpleNamespace(mode="implement", started_at=0, outcome="published")]
    review = ReviewResult(accepted=True, acceptance_met=[True, False])
    return task, result, review, checks, baseline, history


def test_gatekeeper_results_override_builder_claim_and_escape_markdown():
    task, result, review, checks, baseline, history = fixture()
    report, ready, attention = render_report(task, None, result, review, checks, baseline, ["tests/x.py"],
                                             history, "model")
    assert ready is False and any("Gatekeeper checks failed" in a for a in attention)
    assert "| pytest | 1 | 1 | AssertionError |" in report
    assert "\n# Forged" not in report and "<script>" not in report and "a&#124;b" in report
    assert report == SNAPSHOT.read_text()


def test_pr_body_matches_appendix_d():
    task, result, review, checks, _, _ = fixture()
    body = pr_body(task, result, review, checks, ["Gatekeeper checks failed: pytest"], "CrypticFate")
    assert body.startswith("Implements **plans/01-one.md**: One\n\nFull report: `report/01-one.md`")
    assert "### Needs attention\n- Gatekeeper checks failed: pytest" in body
    assert "- [x] Works (met)" in body and "- [ ] Documented (partial: see report)" in body
    assert "- `pytest`: failed (exit 1)" in body
    assert body.rstrip().endswith("Only @CrypticFate's comments are used for revisions.")
