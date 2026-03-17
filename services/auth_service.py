"""
G01: Authentication service — API key generation, wallet signature, and verification.
"""
import hashlib
import secrets
import time
import logging
import re
from functools import wraps
from flask import request, jsonify, g
from sqlalchemy.exc import IntegrityError
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


def verify_wallet_signature(address: str, timestamp: str, signature: str,
                            method: str, path: str) -> bool:
    """Verify an EIP-191 wallet signature for request authentication.

    Signed message: "SYNAI:{METHOD}:{PATH}:{TIMESTAMP}"
    Timestamp must be Unix seconds (UTC). Rejected if older than 5 minutes.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            return False

        message_text = f"SYNAI:{method}:{path}:{timestamp}"
        message = encode_defunct(text=message_text)
        recovered = Account.recover_message(message, signature=signature)
        return recovered.lower() == address.lower()
    except (ValueError, TypeError):
        return False
    except Exception as e:
        logger.warning("Wallet signature verification error: %s", e)
        return False


def get_or_create_agent_by_wallet(wallet_address: str) -> Agent:
    """Look up agent by wallet address, or auto-register a new one.

    Handles concurrent creation via IntegrityError retry.
    """
    from web3 import Web3
    checksummed = Web3.to_checksum_address(wallet_address)
    lowered = checksummed.lower()

    agent = Agent.query.filter(
        db.func.lower(Agent.wallet_address) == lowered
    ).first()
    if agent:
        return agent

    # Auto-register: wallet address as agent_id
    agent = Agent(
        agent_id=lowered,
        name=f"{checksummed[:8]}...{checksummed[-4:]}",
        wallet_address=checksummed,
    )
    try:
        db.session.add(agent)
        db.session.flush()
        logger.info("Auto-registered agent %s from wallet %s", lowered, checksummed)
        return agent
    except IntegrityError:
        db.session.rollback()
        # Concurrent request already created this agent — fetch it
        agent = Agent.query.get(lowered)
        if agent:
            return agent
        # Fallback: query by wallet
        return Agent.query.filter(
            db.func.lower(Agent.wallet_address) == lowered
        ).first()


def authenticate_request() -> Agent | None:
    """Authenticate via Bearer API key or Wallet signature. Returns Agent or None."""
    auth_header = request.headers.get('Authorization', '')

    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
        return verify_api_key(token)

    if auth_header.startswith('Wallet '):
        parts = auth_header[7:].split(':', 2)
        if len(parts) != 3:
            return None
        address, timestamp, signature = parts
        if not re.match(r'^0x[0-9a-fA-F]{40}$', address):
            return None
        if verify_wallet_signature(address, timestamp, signature,
                                   request.method, request.path):
            return get_or_create_agent_by_wallet(address)

    return None


def require_auth(f):
    """Decorator: require valid auth (API key or wallet signature).

    Sets g.current_agent_id on success.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        agent = authenticate_request()
        if not agent:
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

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
