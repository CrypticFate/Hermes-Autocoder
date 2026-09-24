import sys
from decimal import Decimal
from pathlib import Path

import pytest

from autocoder.config import Profile, Settings
from autocoder.db import initialize, session_factory
from autocoder.models import Attempt, Control, Repository, Task

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def settings(tmp_path):
    provider = tmp_path / "provider-secret"
    provider.write_text("test-provider-credential")
    github = tmp_path / "github-secret"
    github.write_text("test-github-credential")
    return Settings(database_url=f"sqlite:///{tmp_path}/state.db", data_dir=tmp_path / "data",
                    owners=["owner"], operator_login="owner",
                    github={"bot_login": "owner-bot", "token_secret": github,
                            "commit_email": "123+owner-bot@users.noreply.github.com"},
                    model={"model": "test-model", "input_usd_per_mtok": Decimal("1"),
                           "output_usd_per_mtok": Decimal("2"), "max_output_tokens": 4096},
                    provider_key_file=provider, budgets={"daily_usd": "10", "monthly_usd": "100"},
                    builder={"image": "sha256:" + "a" * 64}, minimum_free_bytes=0,
                    profiles={"python": Profile(checks=["python -m pytest"])})


@pytest.fixture
def factory(settings):
    factory = session_factory(settings)
    initialize(factory, settings, testing=True)
    with factory.begin() as session:
        session.get(Control, 1).paused = False
        session.add(Repository(id=1, name="owner/repo", owner="owner", enabled=True))
    return factory


@pytest.fixture
def active(factory):
    with factory.begin() as session:
        task = Task(id="task", repo_id=1, title="Fix bug", objective="Fix demonstrated bug",
                    fingerprint="task", state="running", acceptance=["Regression passes"])
        session.add(task)
        session.flush()
        session.add(Attempt(id="attempt", task_id=task.id))
    return "attempt"
