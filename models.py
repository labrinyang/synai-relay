from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
import uuid
from sqlalchemy import JSON, String, Numeric, Text

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
    balance = db.Column(db.Numeric(20, 6), default=0)
    locked_balance = db.Column(db.Numeric(20, 6), default=0) # Staked funds
    # Reputation System
    metrics = db.Column(JSON, default=lambda: {"engineering": 0, "creativity": 0, "reliability": 0})
    
    wallet_address = db.Column(db.String(42))
    encrypted_privkey = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    jobs_claimed = db.relationship('Job', backref='claimed_agent', lazy=True)

class Job(db.Model):
    __tablename__ = 'jobs'
    task_id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Numeric(20, 6), nullable=False)
    buyer_id = db.Column(db.String(100))
    claimed_by = db.Column(db.String(100), db.ForeignKey('agents.agent_id'))
    status = db.Column(db.String(20), default='created')
    # Statuses: 'created', 'funded', 'claimed', 'submitted', 'accepted',
    # 'rejected', 'settled', 'expired', 'cancelled', 'refunded'
    escrow_tx_hash = db.Column(db.String(100)) # Link to on-chain deposit
    signature = db.Column(db.String(200))      # Buyer's cryptographic sign-off
    artifact_type = db.Column(db.String(20), default='CODE') # CODE, DOC, API_CALL, ACTION
    verification_config = db.Column(JSON, default=lambda: {})  # Legacy single config
    verifiers_config = db.Column(JSON, default=lambda: [])    # List of {type, weight, config}

    deposit_amount = db.Column(db.Numeric(20, 6), default=0) # Worker stake (5% of price)
    failure_count = db.Column(db.Integer, default=0)         # Circuit Breaker
    max_retries = db.Column(db.Integer, default=3)           # Max verification retries
    expiry = db.Column(db.DateTime, nullable=True)           # Task expiry timestamp
    chain_task_id = db.Column(db.String(66), nullable=True)  # On-chain bytes32 task ID (0x-prefixed hex)
    verdict_data = db.Column(JSON, nullable=True)            # {score, accepted, evidence_hash, timestamp}
    
    # Knowledge Monetization
    solution_price = db.Column(db.Numeric(20, 6), default=0) # Price to unlock solution
    access_list = db.Column(JSON, default=lambda: [])          # Agent IDs who paid
    
    envelope_json = db.Column(JSON, nullable=True) 
    result_data = db.Column(JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LedgerEntry(db.Model):
    __tablename__ = 'ledger_entries'
    entry_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    source_id = db.Column(db.String(100), nullable=False)
    target_id = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Numeric(20, 6), nullable=False)
    transaction_type = db.Column(db.String(50))
    task_id = db.Column(db.String(36), db.ForeignKey('jobs.task_id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


