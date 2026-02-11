"""
G01: Authentication service — API key generation and verification.
"""
import hashlib
import secrets
from functools import wraps
from flask import request, jsonify, g
from models import db, Agent


def generate_api_key() -> tuple:
    """Generate a new API key. Returns (raw_key, key_hash)."""
    raw_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return raw_key, key_hash


def verify_api_key(raw_key: str) -> Agent:
    """Verify an API key and return the associated Agent, or None."""
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return Agent.query.filter_by(api_key_hash=key_hash).first()


def require_auth(f):
    """Decorator: require valid API key in Authorization header.

    Sets g.current_agent_id on success.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

        token = auth_header[7:]  # strip "Bearer "
        agent = verify_api_key(token)
        if not agent:
            return jsonify({"error": "Invalid API key"}), 401

        g.current_agent_id = agent.agent_id
        return f(*args, **kwargs)
    return decorated


def require_buyer(job):
    """Check that the authenticated agent is the job's buyer. Returns error response or None."""
    if g.current_agent_id != job.buyer_id:
        return jsonify({"error": "Only the job creator can perform this action"}), 403
    return None
