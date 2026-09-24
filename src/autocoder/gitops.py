import os
import subprocess
from pathlib import Path

from autocoder.security import PATTERNS, protected, redact


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, token: str = "", *, commit_name="Hermes Autocoder",
                 commit_email="hermes-autocoder@users.noreply.github.com"):
        self.token = token
        self.commit_name, self.commit_email = commit_name, commit_email

    def run(self, directory: Path, *args, check=True):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ASKPASS": str(Path(__file__).with_name("askpass.py")),
               "AUTOCODER_GIT_TOKEN": self.token, "GIT_AUTHOR_NAME": self.commit_name,
               "GIT_AUTHOR_EMAIL": self.commit_email, "GIT_COMMITTER_NAME": self.commit_name,
               "GIT_COMMITTER_EMAIL": self.commit_email}
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", f"safe.directory={directory}", "-c", "protocol.file.allow=never", *args],
            cwd=directory, env=env, capture_output=True, timeout=180)
        output = result.stdout.decode("utf-8", errors="replace")
        if check and result.returncode:
            raise GitError(redact(result.stderr.decode("utf-8", errors="replace")[-2000:], [self.token]))
        return output

    def prepare(self, directory: Path, repo: str, base: str, branch: str):
        directory.parent.mkdir(parents=True, exist_ok=True)
        if not directory.exists():
            directory.mkdir()
            self.run(directory, "init")
            self.run(directory, "remote", "add", "origin", f"https://github.com/{repo}.git")
        self.run(directory, "fetch", "--no-tags", "origin", base)
        sha = self.run(directory, "rev-parse", "FETCH_HEAD").strip()
        if not self.run(directory, "rev-parse", "--verify", "HEAD", check=False).strip():
            remote = self.run(directory, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").strip()
            if remote:
                self.run(directory, "fetch", "origin", branch)
                self.run(directory, "checkout", "-b", branch, "FETCH_HEAD")
            else:
                self.run(directory, "checkout", "-b", branch, sha)
        return sha

    def changed(self, directory: Path, base: str):
        tracked = self.run(directory, "diff", "--name-only", "-z", base).split("\0")
        untracked = self.run(directory, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        return sorted(set(p for p in tracked + untracked if p))

    def gate(self, directory: Path, base: str, secrets=(), *, kind=None):
        import re
        files = self.changed(directory, base)
        if len(files) > 100:
            return files, ["Change exceeds the 100-file maintenance limit"]
        problems = []
        for name in files:
            path = directory / name
            if kind == "implementation" and name.startswith(("plans/", "report/")):
                problems.append(f"Builder changed operator-owned artifact: {name}")
            if name == ".gitmodules":
                problems.append("Submodule changes are forbidden")
            if protected(name):
                problems.append(f"Protected path: {name}")
            if path.is_symlink():
                problems.append(f"Changed symlink requires review: {name}")
                continue
            if path.exists() and not path.resolve().is_relative_to(directory.resolve()):
                problems.append(f"Path escapes workspace: {name}")
                continue
            if path.is_file():
                if path.stat().st_size > 2_000_000:
                    problems.append(f"Large generated/binary file requires review: {name}")
                    continue
                data = path.read_bytes()
                if b"\x00" in data:
                    problems.append(f"Binary change requires review: {name}")
                text = data.decode("utf-8", errors="replace")
                if path.name == ".gitattributes" and re.search(r"(?:filter|diff)\s*=", text):
                    problems.append("Git attribute drivers are forbidden")
                if any(re.search(pattern, text) for pattern in PATTERNS) or any(s and s in text for s in secrets):
                    problems.append(f"Possible secret in {name}")
        return files, problems

    def sensitive(self, files):
        import re
        return [p for p in files if re.search(
            r"(^|/)(tests|__tests__|\.github)/|(^|/)(test_[^/]+\.py|conftest\.py|pytest\.ini|"
            r"Makefile|Dockerfile[^/]*|setup\.py|pyproject\.toml|package\.json|.*lock.*|requirements[^/]*|"
            r"jest\.config\..*|vitest\.config\..*|compose.*\.ya?ml|docker-compose.*)$|"
            r"(_test\.go|\.(test|spec)\.[^/]+)$", p)]

    def tree_digest(self, directory: Path):
        import hashlib
        digest = hashlib.sha256()
        names = self.run(directory, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
        for name in sorted(set(names) - {""}):
            path = directory / name
            digest.update(name.encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                if not path.resolve().is_relative_to(directory.resolve()):
                    raise GitError("Tracked path escapes workspace")
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
            else:
                digest.update(b"DELETED")
        return digest.hexdigest()

    def commit(self, directory: Path, message: str):
        self.run(directory, "add", "--all")
        if self.run(directory, "diff", "--cached", "--name-only").strip():
            self.run(directory, "commit", "-m", message)
        return self.run(directory, "rev-parse", "HEAD").strip()

    def push(self, directory: Path, branch: str):
        if not branch.startswith("agent/"):
            raise GitError("Only agent branches may be pushed")
        self.run(directory, "push", "origin", f"HEAD:refs/heads/{branch}")
