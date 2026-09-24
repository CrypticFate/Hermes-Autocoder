"""Initial controller schema, frozen independently of future ORM changes."""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

schema = sa.MetaData()


def column(name, type_=sa.String, nullable=False, **kwargs):
    return sa.Column(name, type_, nullable=nullable, **kwargs)


sa.Table("control", schema,
    column("id", sa.Integer, primary_key=True), column("paused", sa.Boolean),
    column("daily_micro", sa.BigInteger, nullable=True), column("monthly_micro", sa.BigInteger, nullable=True),
    column("heartbeat", sa.Float), column("discovered_at", sa.Float), column("polled_at", sa.Float),
    column("lease_owner", nullable=True), column("lease_until", sa.Float))
sa.Table("repositories", schema,
    column("id", sa.BigInteger, primary_key=True), column("name", unique=True), column("owner"),
    column("default_branch"), column("enabled", sa.Boolean), column("manual_enabled", sa.Boolean, nullable=True),
    column("accessible", sa.Boolean), column("reason", sa.Text), column("scanned_at", sa.Float),
    column("scanned_sha", nullable=True), column("scanned_context_hash", nullable=True),
    column("lease_owner", nullable=True), column("lease_until", sa.Float))
sa.Table("tasks", schema,
    column("id", primary_key=True),
    sa.Column("repo_id", sa.BigInteger, sa.ForeignKey("repositories.id"), nullable=False, index=True),
    column("plan_id"), column("title"), column("objective", sa.Text), column("evidence", sa.Text),
    column("acceptance", sa.JSON), column("validation_commands", sa.JSON),
    column("dependencies", sa.JSON), column("fingerprint"), column("source"),
    column("state", index=True), column("reason", sa.Text), column("branch", nullable=True),
    column("base_sha", nullable=True), column("tested_sha", nullable=True),
    column("pr_number", sa.Integer, nullable=True), column("pr_url", nullable=True),
    column("attempt_count", sa.Integer), column("feedback", sa.Text), column("feedback_cursor", sa.Float),
    column("retry_at", sa.Float), column("created_at", sa.Float), column("updated_at", sa.Float),
    sa.UniqueConstraint("repo_id", "fingerprint"))
sa.Table("attempts", schema,
    column("id", primary_key=True),
    sa.Column("task_id", sa.String, sa.ForeignKey("tasks.id"), nullable=False, index=True),
    column("mode"), column("started_at", sa.Float), column("finished_at", sa.Float, nullable=True),
    column("outcome"), column("container_id", nullable=True), column("workspace", nullable=True),
    column("setup", sa.JSON), column("baseline", sa.JSON), column("checks", sa.JSON),
    column("changed_files", sa.JSON), column("report_path", nullable=True),
    column("publication_sha", nullable=True), column("ready_for_review", sa.Boolean), column("detail", sa.Text))
sa.Table("events", schema,
    column("id", sa.Integer, primary_key=True, autoincrement=True), column("task_id", nullable=True, index=True),
    column("at", sa.Float), column("kind"), column("detail", sa.Text))
sa.Table("capabilities", schema,
    column("digest", primary_key=True),
    sa.Column("attempt_id", sa.String, sa.ForeignKey("attempts.id"), nullable=False, index=True),
    column("expires_at", sa.Float), column("revoked", sa.Boolean))
sa.Table("charges", schema,
    column("id", primary_key=True),
    sa.Column("attempt_id", sa.String, sa.ForeignKey("attempts.id"), nullable=False, index=True),
    column("created_at", sa.Float, index=True), column("micro_usd", sa.BigInteger),
    column("reserved_micro", sa.BigInteger), column("input_tokens", sa.Integer, nullable=True),
    column("output_tokens", sa.Integer, nullable=True), column("state"))


def upgrade():
    schema.create_all(op.get_bind())


def downgrade():
    schema.drop_all(op.get_bind())
