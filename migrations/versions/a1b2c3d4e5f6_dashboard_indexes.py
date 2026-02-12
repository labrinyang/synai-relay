"""Add dashboard performance indexes

Revision ID: a1b2c3d4e5f6
Revises: 2849e33ec682
Create Date: 2026-02-12
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '2849e33ec682'
branch_labels = None
depends_on = None


def upgrade():
    # Leaderboard sorting
    op.create_index('ix_agents_total_earned', 'agents', ['total_earned'])
    op.create_index('ix_agents_completion_rate', 'agents', ['completion_rate'])
    # Winner task count
    op.create_index('ix_jobs_winner_id', 'jobs', ['winner_id'])
    # Settlement status stats
    op.create_index('ix_jobs_payout_status', 'jobs', ['payout_status'])
    # Hot tasks (active participants)
    op.create_index('ix_job_participants_active', 'job_participants', ['task_id', 'unclaimed_at'])
    # Idempotency key cleanup
    op.create_index('ix_idempotency_created', 'idempotency_keys', ['created_at'])


def downgrade():
    op.drop_index('ix_idempotency_created', table_name='idempotency_keys')
    op.drop_index('ix_job_participants_active', table_name='job_participants')
    op.drop_index('ix_jobs_payout_status', table_name='jobs')
    op.drop_index('ix_jobs_winner_id', table_name='jobs')
    op.drop_index('ix_agents_completion_rate', table_name='agents')
    op.drop_index('ix_agents_total_earned', table_name='agents')
