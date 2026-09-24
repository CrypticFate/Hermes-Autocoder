import random

import pytest
from sqlalchemy import select

from autocoder.models import Repository, Task
from autocoder.scheduler import ACTIVE, TRANSITIONS, control_plan, next_task, transition


def task(seq, state="pending"):
    return Task(id=f"p{seq}", repo_id=1, plan_path=f"plans/{seq:02d}-work.md", plan_seq=seq,
                plan_slug="work", title="Work", objective="Work", fingerprint=f"p{seq}", state=state)


def test_random_predecessor_states_never_allow_early_plan(factory):
    randomizer = random.Random(42)
    states = ["merged", "skipped", "cancelled", "blocked", "invalid", "closed_unmerged", "pending", *ACTIVE]
    for _ in range(100):
        with factory() as session:
            rows = [task(i, randomizer.choice(states)) for i in range(1, 5)]
            session.add_all(rows)
            session.flush()
            selected = next_task(session, session.get(Repository, 1))
            if selected:
                assert not any(t.state in ACTIVE for t in rows)
                assert all(t.state in {"merged", "skipped", "cancelled"}
                           for t in rows if t.plan_seq < selected.plan_seq)
            session.rollback()


@pytest.mark.parametrize("source", sorted(TRANSITIONS))
def test_transition_table(factory, source):
    for target in TRANSITIONS:
        with factory() as session:
            row = task(1, source)
            session.add(row)
            session.flush()
            if target == source or target in TRANSITIONS[source]:
                transition(session, row, target, "test")
                assert row.state == target
            else:
                with pytest.raises(ValueError, match="Illegal"):
                    transition(session, row, target)
            session.rollback()


def test_close_pauses_and_retry_uses_new_branch(factory):
    with factory.begin() as session:
        row = task(1, "pr_open")
        row.pr_number = 12
        session.add(row)
        session.flush()
        transition(session, row, "closed_unmerged")
        assert session.get(Repository, 1).queue_state == "paused"
    control_plan(factory, "owner/repo", 1, retry=True)
    with factory() as session:
        row = session.scalar(select(Task))
        assert row.branch == "agent/01-work-r1"
        assert row.pr_number is None
        assert row.state == "queued"
