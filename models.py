from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import uuid

db = SQLAlchemy()


class Owner(db.Model):
    __tablename__ = 'owners'
    owner_id = db.Column(db.String(100), primary_key=True)
    username = db.Column(db.String(100), nullable=False)
    twitter_handle = db.Column(db.String(100))
    avatar_url = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    agents = db.relationship('Agent', backref='owner', lazy=True)


class Agent(db.Model):
    __tablename__ = 'agents'
    agent_id = db.Column(db.String(100), primary_key=True)
    owner_id = db.Column(db.String(100), db.ForeignKey('owners.owner_id'))
    name = db.Column(db.String(100), nullable=False)
    adopted_at = db.Column(db.DateTime)
    is_ghost = db.Column(db.Boolean, default=False)
    adoption_tweet_url = db.Column(db.Text)
    adoption_hash = db.Column(db.String(64))
    # Reputation (replaces balance/locked_balance)
    metrics = db.Column(db.JSON, default=lambda: {"engineering": 0, "creativity": 0, "reliability": 0})
    completion_rate = db.Column(db.Numeric(5, 4), nullable=True)  # 0.0000-1.0000
    total_earned = db.Column(db.Numeric(20, 6), default=0)
    # Wallet
    wallet_address = db.Column(db.String(42))
    encrypted_privkey = db.Column(db.Text)
    # Auth (G01)
    api_key_hash = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Job(db.Model):
    __tablename__ = 'jobs'
    task_id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text)
    rubric = db.Column(db.Text, nullable=True)
    price = db.Column(db.Numeric(20, 6), nullable=False)
    buyer_id = db.Column(db.String(100), db.ForeignKey('agents.agent_id'))  # G08: FK
    status = db.Column(db.String(20), default='open', index=True)  # G09: index
    # Statuses: 'open', 'funded', 'resolved', 'expired', 'cancelled'
    artifact_type = db.Column(db.String(20), default='GENERAL')
    # On-chain deposit
    deposit_tx_hash = db.Column(db.String(100), unique=True, nullable=True)
    depositor_address = db.Column(db.String(42), nullable=True)
    # Payout/refund
    payout_tx_hash = db.Column(db.String(100), nullable=True)
    payout_status = db.Column(db.String(20), nullable=True)  # G06: pending|success|failed|skipped
    fee_tx_hash = db.Column(db.String(100), nullable=True)
    refund_tx_hash = db.Column(db.String(100), nullable=True)
    winner_id = db.Column(db.String(100), db.ForeignKey('agents.agent_id'), nullable=True)
    # Multi-worker
    participants = db.Column(db.JSON, default=lambda: [])
    # Oracle
    oracle_config = db.Column(db.JSON, default=lambda: {})
    min_reputation = db.Column(db.Numeric(5, 4), nullable=True)
    max_submissions = db.Column(db.Integer, default=20)
    max_retries = db.Column(db.Integer, default=3)
    # Fee (G19)
    fee_bps = db.Column(db.Integer, default=2000)  # basis points: 2000 = 20%
    # Lifecycle
    failure_count = db.Column(db.Integer, default=0)
    expiry = db.Column(db.DateTime, nullable=True)
    # Knowledge monetization
    solution_price = db.Column(db.Numeric(20, 6), default=0)
    access_list = db.Column(db.JSON, default=lambda: [])
    # Data
    envelope_json = db.Column(db.JSON, nullable=True)
    result_data = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Relationships
    submissions = db.relationship('Submission', backref='job', lazy=True,
                                  foreign_keys='Submission.task_id')

    # G09: indexes
    __table_args__ = (
        db.Index('ix_jobs_buyer_id', 'buyer_id'),
        db.Index('ix_jobs_status_created', 'status', 'created_at'),
    )


class Submission(db.Model):
    __tablename__ = 'submissions'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = db.Column(db.String(36), db.ForeignKey('jobs.task_id'), nullable=False, index=True)  # G09
    worker_id = db.Column(db.String(100), db.ForeignKey('agents.agent_id'), nullable=False, index=True)  # G09
    content = db.Column(db.JSON)
    status = db.Column(db.String(20), default='pending')
    # Statuses: 'pending', 'judging', 'passed', 'failed'
    oracle_score = db.Column(db.Integer, nullable=True)
    oracle_reason = db.Column(db.Text, nullable=True)
    oracle_steps = db.Column(db.JSON, nullable=True)
    attempt = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Relationships
    worker = db.relationship('Agent', foreign_keys=[worker_id])

    # G09: composite index for retry count queries
    __table_args__ = (
        db.Index('ix_submissions_task_worker', 'task_id', 'worker_id'),
    )


class IdempotencyKey(db.Model):
    """G17: Idempotency keys for safe request retries."""
    __tablename__ = 'idempotency_keys'
    key = db.Column(db.String(128), primary_key=True)
    agent_id = db.Column(db.String(100), nullable=False)
    response_code = db.Column(db.Integer, nullable=False)
    response_body = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Webhook(db.Model):
    """G04: Webhook registration for event push notifications."""
    __tablename__ = 'webhooks'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id = db.Column(db.String(100), db.ForeignKey('agents.agent_id'), nullable=False, index=True)
    url = db.Column(db.Text, nullable=False)
    events = db.Column(db.JSON, default=lambda: [])  # e.g., ["job.resolved", "submission.completed"]
    secret = db.Column(db.String(64), nullable=True)  # HMAC secret for signature
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
