"""add payout_error to jobs

Revision ID: a3e99bf9f17a
Revises: b7c8d9e0f1a2
Create Date: 2026-02-14 16:15:11.365095

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3e99bf9f17a'
down_revision = 'b7c8d9e0f1a2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('payout_error', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('payout_error')
