"""Add v2 state without rewriting or dropping v1 rows."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = depends_on = None

ADDITIONS = {
    "repositories": [("onboarded_at", sa.DateTime(timezone=True), None), ("queue_state", sa.String(), "active"),
        ("ruleset_verified_at", sa.DateTime(timezone=True), None),
        ("ruleset_report", sa.JSON().with_variant(JSONB(), "postgresql"), None),
        ("last_plans_scan_sha", sa.String(), None)],
    "tasks": [("kind", sa.String(), "implementation"), ("plan_path", sa.String(), None),
        ("plan_seq", sa.Integer(), None), ("plan_slug", sa.String(), None),
        ("plan_blob_sha", sa.String(), None), ("plan_title", sa.String(), None),
        ("services", sa.JSON().with_variant(JSONB(), "postgresql"), "[]"),
        ("extra_checks", sa.JSON().with_variant(JSONB(), "postgresql"), "[]"),
        ("report_path", sa.String(), None), ("repair_count", sa.Integer(), "0"),
        ("invalid_reason", sa.Text(), None), ("feedback_cursors", sa.JSON(), "{}"),
        ("retry_count", sa.Integer(), "0")],
    "capabilities": [("kind", sa.String(), "attempt"), ("pool", sa.String(), "builder")],
    "charges": [("pool", sa.String(), "builder")],
}
STATES = "'queued','planning','ready','planned','running','validating','pr_open','awaiting_review'," \
         "'blocked','failed','cancelled','closed_unmerged','merged','pending','preparing','publishing'," \
         "'changes_requested','skipped','invalid'"


def upgrade():
    for table, fields in ADDITIONS.items():
        with op.batch_alter_table(table) as batch:
            for name, kind, default in fields:
                batch.add_column(sa.Column(name, kind, nullable=default is None, server_default=default))
            if table in {"capabilities", "charges"}:
                batch.alter_column("attempt_id", existing_type=sa.String(), nullable=True)
            if table == "tasks":
                batch.create_check_constraint("task_state_v2", f"state IN ({STATES})")
    op.create_index("uq_task_plan", "tasks", ["repo_id", "plan_path"], unique=True,
                    sqlite_where=sa.text("kind = 'implementation'"),
                    postgresql_where=sa.text("kind = 'implementation'"))
    op.create_table("pr_feedback",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("github_id", sa.String(), nullable=False), sa.Column("source", sa.String(), nullable=False),
        sa.Column("author_login", sa.String(), nullable=False), sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("consumed_by_attempt_id", sa.String(), sa.ForeignKey("attempts.id")),
        sa.Column("ignored", sa.Boolean(), nullable=False), sa.UniqueConstraint("task_id", "source", "github_id"))
    op.create_table("sidecars", sa.Column("id", sa.String(), primary_key=True),
        sa.Column("attempt_id", sa.String(), sa.ForeignKey("attempts.id"), nullable=False),
        sa.Column("service_name", sa.String(), nullable=False), sa.Column("image", sa.String(), nullable=False),
        sa.Column("container_id", sa.String()), sa.Column("network_id", sa.String()),
        sa.Column("status", sa.String(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("removed_at", sa.Float()))
    op.create_table("notifications", sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.Float(), nullable=False), sa.Column("level", sa.String(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), sa.ForeignKey("repositories.id")),
        sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.id")), sa.Column("message", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.Float()), sa.Column("dedupe_key", sa.String(), unique=True))


def downgrade():
    # Refuse a lossy downgrade after concierge accounting has begun.
    for table in ("capabilities", "charges"):
        if op.get_bind().execute(sa.text(f"SELECT count(*) FROM {table} WHERE attempt_id IS NULL")).scalar():
            raise RuntimeError("Export concierge accounting before downgrading; rows will not be deleted")
    for table in ("notifications", "sidecars", "pr_feedback"):
        op.drop_table(table)
    op.drop_index("uq_task_plan", table_name="tasks")
    for table, fields in reversed(list(ADDITIONS.items())):
        with op.batch_alter_table(table) as batch:
            if table == "tasks":
                batch.drop_constraint("task_state_v2", type_="check")
            if table in {"capabilities", "charges"}:
                batch.alter_column("attempt_id", existing_type=sa.String(), nullable=False)
            for name, _, _ in reversed(fields):
                batch.drop_column(name)
