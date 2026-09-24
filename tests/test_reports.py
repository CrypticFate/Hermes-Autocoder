from types import SimpleNamespace

from autocoder.contracts import ReviewResult, RunResult
from autocoder.reports import render_report


def test_gatekeeper_results_override_builder_claim_and_escape_markdown():
    task = SimpleNamespace(plan_seq=1, title="One", plan_path="plans/01-one.md", plan_blob_sha="blob",
        branch="agent/01-one", base_sha="base", acceptance=["Works"], services=[])
    result = RunResult(summary="All tests pass\n# Forged heading<script>", criteria=[
        {"criterion": "Works", "status": "met", "evidence": "a|b"}])
    checks = [{"command": "pytest", "exit_code": 1, "output": "AssertionError"}]
    report, ready, attention = render_report(task, None, result,
        ReviewResult(accepted=True, acceptance_met=[True]), checks, [], [], [], "model")
    assert ready is False and "Gatekeeper checks failed" in attention
    assert "| pytest | not run | 1 | AssertionError |" in report
    assert "\n# Forged" not in report and "<script>" not in report and "a&#124;b" in report
