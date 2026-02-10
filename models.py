from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import uuid
from sqlalchemy import JSON, String, Numeric, Text
import enum

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
    status = db.Column(db.String(20), default='posted') # 'posted', 'funded', 'claimed', 'submitted', 'completed'
    escrow_tx_hash = db.Column(db.String(100)) # Link to on-chain deposit
    signature = db.Column(db.String(200))      # Buyer's cryptographic sign-off
    # Updated Schema for "Synapse Gateway"
    artifact_type = db.Column(db.String(20), default='CODE') # CODE, DOC, API_CALL, ACTION
    verification_config = db.Column(JSON, default={})        # Stores rubric, webhooks, constraints
    
    envelope_json = db.Column(JSON, nullable=True) # Deprecated but kept for migration safety
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


