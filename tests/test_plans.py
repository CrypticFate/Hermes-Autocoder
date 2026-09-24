import pytest
from sqlalchemy import select

from autocoder.gitops import Git
from autocoder.models import Notification, Repository, Task
from autocoder.plans import InvalidPlan, discover, parse, reconcile, render_plan, request_plan_draft


def text():
    return "# First plan\n\n## Objective\nBuild a thing\n\n## Acceptance criteria\n- [ ] Works\n"


@pytest.mark.parametrize("change,error", [
    (lambda t: t.replace("# First plan", ""), "heading"),
    (lambda t: t.replace("Build a thing", ""), "Objective"),
    (lambda t: t.replace("- [ ] Works", ""), "Acceptance"),
    (lambda t: "---\nservices: [unknown]\n---\n" + t, "Unknown services"),
    (lambda t: "---\nchecks: command\n---\n" + t, "list"),
])
def test_parser_rejects_invalid(change, error):
    with pytest.raises(ValueError, match=error):
        parse("plans/01-first.md", change(text()))


def test_frontmatter_title_and_filename():
    plan = parse("plans/01-first.md", "---\ntitle: Metadata title\n---\n" + text().replace("# First plan", ""))
    assert plan.title == "Metadata title"
    with pytest.raises(ValueError, match="Filename"):
        parse("plans/README.md", text())
    path, body = render_plan(2, "Second plan", "Build", ["Works"])
    assert parse(path, body).acceptance_criteria == ["Works"]


def test_discovery_reads_committed_blobs_and_rejects_duplicates(tmp_path):
    git = Git()
    git.run(tmp_path, "init")
    (tmp_path / "plans").mkdir()
    first = tmp_path / "plans/01-first.md"
    first.write_text(text())
    head = git.commit(tmp_path, "Plans")
    first.write_text("uncommitted invalid edit")
    assert discover(tmp_path, head)[0].title == "First plan"
    first.write_text(text())
    (tmp_path / "plans/001-other.md").write_text(text())
    head = git.commit(tmp_path, "Duplicate")
    assert all(isinstance(p, InvalidPlan) and "Duplicate" in p.reason for p in discover(tmp_path, head))


def test_reconcile_lifecycle_and_empty_draft_deduplication(factory):
    plan = parse("plans/01-first.md", text(), "blob1")
    with factory.begin() as session:
        repo = session.get(Repository, 1)
        reconcile(session, repo, [plan])
        reconcile(session, repo, [plan])
        assert len(session.scalars(select(Task)).all()) == 1
        task = session.scalar(select(Task))
        newer = plan.model_copy(update={"blob_sha": "blob2", "title": "Updated"})
        reconcile(session, repo, [newer])
        assert task.title == "Updated"
        task.attempt_count = 1
        reconcile(session, repo, [plan])
        assert task.plan_blob_sha == "blob2"
        assert session.scalar(select(Notification))
        task.attempt_count = 0
        reconcile(session, repo, [])
        assert task.state == "cancelled"
    one = request_plan_draft(factory, "owner/repo", explicit=False)
    assert request_plan_draft(factory, "owner/repo", explicit=False) == one
