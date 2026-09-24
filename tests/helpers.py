"""Deterministic doubles for end-to-end controller tests: real git, fake Docker/GitHub/Hermes."""
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

from autocoder.contracts import ReviewResult, RunResult
from autocoder.gitops import Git

PLAN = """# {title}

## Objective
{objective}

## Acceptance criteria
- [ ] {criterion}
"""


class LocalGit(Git):
    """Real git against a local bare repository standing in for github.com."""
    allow_file_protocol = True

    def __init__(self, remote_root, pushes=None):
        super().__init__("local-test-token", commit_name="Hermes Autocoder",
                         commit_email="1+owner-bot@users.noreply.github.com")
        self.remote_root = Path(remote_root)
        self.pushes = [] if pushes is None else pushes

    def remote_url(self, repo):
        return str(self.remote_root / f"{repo}.git")

    def push(self, directory, branch):
        super().push(directory, branch)
        self.pushes.append(branch)


def make_remote(tmp_path, files, repo="owner/repo"):
    """Create a bare remote with an initial main commit containing files."""
    remote = tmp_path / "remote"
    bare = remote / f"{repo}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    write_files(seed, files)
    commit_and_push(seed, bare, "seed")
    return remote, seed, bare


def write_files(root, files):
    for path, text in files.items():
        target = Path(root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def commit_and_push(seed, bare, message):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Operator", "GIT_AUTHOR_EMAIL": "op@example.invalid",
           "GIT_COMMITTER_NAME": "Operator", "GIT_COMMITTER_EMAIL": "op@example.invalid"}
    subprocess.run(["git", "-C", str(seed), "add", "--all"], check=True, env=env)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", message], check=True, env=env)
    subprocess.run(["git", "-C", str(seed), "push", "-q", str(bare), "HEAD:refs/heads/main"], check=True, env=env)


def git_log(bare, ref):
    return subprocess.run(["git", "--git-dir", str(bare), "log", "--format=%s|%an", ref],
                          capture_output=True, text=True, check=True).stdout.splitlines()


def show(bare, ref, path):
    return subprocess.run(["git", "--git-dir", str(bare), "show", f"{ref}:{path}"],
                          capture_output=True, text=True).stdout


class FakeNetwork:
    def __init__(self, name):
        self.name, self.id = name, name
        self.labels = {"autocoder.managed": "true", "autocoder.attempt": name.removeprefix("att-")}
        self.attrs = {"Internal": True, "Labels": self.labels}
        self.removed = False

    def remove(self):
        self.removed = True

    def connect(self, *args, **kwargs):
        pass

    def disconnect(self, *args, **kwargs):
        pass


class FakeDocker:
    def __init__(self):
        self.networks = Mock()
        self.created = []

        def create(name, **kwargs):
            assert kwargs["internal"] is True
            network = FakeNetwork(name)
            self.created.append(network)
            return network
        self.networks.create.side_effect = create
        self.networks.list.return_value = []
        self.containers = Mock()
        self.containers.list.return_value = []


class FixtureRunner:
    """Stands in for DockerRunner. `behaviour(context, workspace)` edits the workspace like a builder."""
    contexts = []
    behaviour = None
    review = ReviewResult(accepted=True, acceptance_met=[True])

    def __init__(self, settings, workspace, run_dir, profile, token, heartbeat, client=None):
        self.workspace, self.run_dir, self.token, self.heartbeat = workspace, run_dir, token, heartbeat
        self.profile, self.network, self.service_env, self.check_only = profile, None, {}, False

    def start(self, attempt_id):
        return f"fixture-{attempt_id}"

    def setup(self):
        return []

    def command(self, argv, timeout=None, environment=None):
        original = argv
        if isinstance(argv, str):
            argv = ["sh", "-c", argv]
        result = subprocess.run(argv, cwd=self.workspace, capture_output=True, text=True, timeout=30,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return {"command": original, "exit_code": result.returncode, "output": result.stdout + result.stderr}

    def run(self, context):
        assert self.token, "a capability token must be issued before Hermes runs"
        self.heartbeat()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "context.json").write_text(context.model_dump_json())
        FixtureRunner.contexts.append(context)
        if context.mode == "review":
            return RunResult(completed=True, review=FixtureRunner.review)
        if FixtureRunner.behaviour:
            return FixtureRunner.behaviour(context, self.workspace)
        return default_behaviour(context, self.workspace)

    def stop(self):
        pass


def default_behaviour(context, workspace):
    if context.kind == "plan_draft":
        write_files(workspace, {"plans/01-first-step.md": PLAN.format(
            title="First step", objective="Build the first step", criterion="It works")})
        return RunResult(completed=True, summary="Drafted one plan")
    marker = workspace / "src" / f"{context.plan_path.split('/')[-1].removesuffix('.md')}.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"implemented {context.title} {len(FixtureRunner.contexts)}\n")
    return RunResult(completed=True, summary=f"Implemented {context.title}. All tests pass.",
                     criteria=[{"criterion": c, "status": "met", "evidence": "marker file"}
                               for c in context.criteria],
                     files_changed_rationale=[{"path": str(marker.relative_to(workspace)), "why": "plan"}])


def github_double(pr_number=1):
    client = Mock()
    client.verify_identity.return_value = "owner-bot"
    client.repository.return_value = {"id": 1, "default_branch": "main", "archived": False}
    client.permission.return_value = {"permission": "write"}
    client.branch.return_value = {"name": "main"}
    client.branch_rules.return_value = [
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1,
                                                 "require_last_push_approval": True}},
        {"type": "deletion"}, {"type": "non_fast_forward"}]
    counter = iter(range(pr_number, 1000))

    def ensure_pr(repo, branch, base, title, body, draft):
        number = next(counter)
        client.opened.append({"branch": branch, "title": title, "body": body, "draft": draft, "number": number})
        return {"number": number, "html_url": f"https://github.com/{repo}/pull/{number}", "state": "open",
                "draft": draft}
    client.opened = []
    client.ensure_pr.side_effect = ensure_pr
    client.issue_comments.return_value = []
    client.review_comments.return_value = []
    client.reviews.return_value = []
    client.check_runs.return_value = []
    return client
