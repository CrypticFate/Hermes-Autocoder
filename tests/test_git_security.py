import subprocess

import pytest

from autocoder.gitops import Git, GitError
from autocoder.security import redact, safe_write


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "code.py").write_text("value = 1\n")
    git = Git()
    base = git.commit(tmp_path, "baseline")
    return tmp_path, git, base


def test_diff_gate_protected_paths_and_secret(repository):
    root, git, base = repository
    directory = root / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / "ci.yaml").write_text("disabled: true\n")
    (root / "credentials.py").write_text('token = "ghp_' + 'x' * 36 + '"\n')
    files, problems = git.gate(root, base)
    assert len(files) == 2
    assert ".github/workflows/ci.yaml" in git.sensitive(files)
    assert any("Possible secret" in p for p in problems)


def test_changed_symlink_is_blocked(repository):
    root, git, base = repository
    (root / "escape").symlink_to("/etc/passwd")
    _, problems = git.gate(root, base)
    assert any("symlink" in p for p in problems)


def test_artifact_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "report").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        safe_write(root, "report/leak.md", "bad")
    assert not (tmp_path / "leak.md").exists()


def test_only_agent_branches_can_be_pushed(repository):
    root, git, _ = repository
    with pytest.raises(GitError, match="Only agent"):
        git.push(root, "main")


def test_redaction():
    assert "private-secret" not in redact("key=private-secret", ["private-secret"])
    assert "ghp_" not in redact("ghp_" + "x" * 36)
