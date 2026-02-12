"""
G01: Authentication service — API key generation and verification.
"""
import hashlib
import secrets
import time
import logging
from functools import wraps
from flask import request, jsonify, g
from models import db, Agent

logger = logging.getLogger(__name__)


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


def verify_operator_signature(signature_hex, timestamp, path):
    """Verify an Operator signature against OPERATOR_ADDRESS.

    The signed message is: SYNAI:{path}:{timestamp}
    Returns (is_valid, error_message).
    """
    from config import Config
    from eth_account import Account
    from eth_account.messages import encode_defunct

    operator_addr = Config.OPERATOR_ADDRESS
    if not operator_addr:
        logger.error("OPERATOR_ADDRESS not configured")
        return False, "Operator verification not configured"

    # Anti-replay: check timestamp freshness
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False, "Invalid timestamp"

    drift = time.time() - ts
    if drift < -30:  # Allow 30s clock skew
        return False, "Timestamp is in the future"
    max_age = Config.OPERATOR_SIGNATURE_MAX_AGE
    if drift > max_age:
        return False, f"Signature expired ({int(drift)}s > {max_age}s)"

    # Recover signer from signature
    message_text = f"SYNAI:{path}:{timestamp}"
    try:
        message = encode_defunct(text=message_text)
        recovered = Account.recover_message(message, signature=signature_hex)
    except Exception as e:
        logger.warning("Operator signature recovery failed: %s", e)
        return False, "Invalid signature format"

    if recovered.lower() != operator_addr.lower():
        logger.warning("Operator signature mismatch: recovered=%s expected=%s", recovered, operator_addr)
        return False, "Signature does not match operator"

    return True, None


def require_operator(f):
    """Decorator: require valid Operator signature.

    Expects headers:
      X-Operator-Signature: <hex signature>
      X-Operator-Timestamp: <unix timestamp>

    Signed message format: SYNAI:{request.path}:{timestamp}

    NOTE: Anti-replay uses timestamp freshness only (no nonce).
    Safe for idempotent/read-only endpoints. If applied to state-mutating
    endpoints, add a nonce or idempotency-key to the signed message.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        signature = request.headers.get('X-Operator-Signature')
        timestamp = request.headers.get('X-Operator-Timestamp')

        if not signature or not timestamp:
            return jsonify({"error": "Operator signature required"}), 401

        valid, error = verify_operator_signature(signature, timestamp, request.path)
        if not valid:
            return jsonify({"error": error}), 403

        g.operator_verified = True
        return f(*args, **kwargs)
    return decorated


def require_buyer(job):
    """Check that the authenticated agent is the job's buyer. Returns error response or None."""
    if g.current_agent_id != job.buyer_id:
        return jsonify({"error": "Only the job creator can perform this action"}), 403
    return None
