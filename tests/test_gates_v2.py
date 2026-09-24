import pytest

from autocoder.gitops import Git


@pytest.mark.parametrize("path,body", [("plans/01-change.md", "bad"), ("report/01-change.md", "bad"),
    (".gitmodules", "submodule"), (".gitattributes", "*.py diff=exec")])
def test_builder_artifact_and_driver_gates(tmp_path, path, body):
    git = Git()
    git.run(tmp_path, "init")
    (tmp_path / "README.md").write_text("Baseline")
    base = git.commit(tmp_path, "baseline")
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    assert git.gate(tmp_path, base, kind="implementation")[1]


def test_sensitive_paths_are_flags_not_hard_gates(tmp_path):
    git = Git()
    paths = ["tests/test_app.py", ".github/workflows/ci.yml", "package.json", "src/app.spec.ts"]
    assert git.sensitive(paths) == paths
    assert git.sensitive(["src/app.py", "README.md"]) == []
