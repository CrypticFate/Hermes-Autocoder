"""Track repair feedback consumption and rebased replacement pull requests."""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = depends_on = None

ADDITIONS = {
    "tasks": [("superseded_pr_number", sa.Integer(), None), ("rebase_count", sa.Integer(), "0")],
    "attempts": [("feedback_ids", sa.JSON(), "[]")],
}


def upgrade():
    for table, fields in ADDITIONS.items():
        with op.batch_alter_table(table) as batch:
            for name, kind, default in fields:
                batch.add_column(sa.Column(name, kind, nullable=default is None, server_default=default))


def downgrade():
    for table, fields in reversed(list(ADDITIONS.items())):
        with op.batch_alter_table(table) as batch:
            for name, _, _ in reversed(fields):
                batch.drop_column(name)
