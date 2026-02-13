"""Add submission (task_id, status) composite index

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-02-13
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b7c8d9e0f1a2'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # Supports GROUP BY (task_id) + CASE(status) aggregation in to_dict_batch()
    # and individual status COUNT queries in to_dict()
    with op.get_context().connection.begin_nested():
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_submissions_task_status "
            "ON submissions (task_id, status)"
        )


def downgrade():
    op.drop_index('ix_submissions_task_status', table_name='submissions')
