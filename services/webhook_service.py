"""
G04: Webhook service — registration, delivery, HMAC signing.
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import requests as http_requests

from models import db, Webhook, JobParticipant

logger = logging.getLogger('relay.webhooks')

# Max retries for webhook delivery
MAX_RETRIES = 3
# Backoff multiplier (seconds): 1, 2, 4
BACKOFF_BASE = 1


def is_safe_webhook_url(url: str) -> bool:
    """C1 fix: Validate webhook URL is not targeting internal infrastructure."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False
        # Resolve hostname and check IP is globally routable
        ip = ipaddress.ip_address(socket.gethostbyname(hostname))
        return ip.is_global
    except (socket.gaierror, ValueError, OSError):
        return False


MAX_WEBHOOKS_PER_AGENT = 10


def create_webhook(agent_id: str, url: str, events: list) -> dict:
    """Register a new webhook for an agent."""
    # m5 fix: Limit webhook count per agent
    active_count = Webhook.query.filter_by(agent_id=agent_id, active=True).count()
    if active_count >= MAX_WEBHOOKS_PER_AGENT:
        raise ValueError(f"Maximum {MAX_WEBHOOKS_PER_AGENT} webhooks per agent")

    secret = secrets.token_hex(32)
    wh = Webhook(
        agent_id=agent_id,
        url=url,
        events=events or [],
        secret=secret,
        active=True,
    )
    db.session.add(wh)
    db.session.commit()
    return _to_dict(wh, include_secret=True)


def list_webhooks(agent_id: str) -> list:
    """List all webhooks for an agent."""
    hooks = Webhook.query.filter_by(agent_id=agent_id, active=True).all()
    return [_to_dict(wh) for wh in hooks]


def delete_webhook(webhook_id: str, agent_id: str) -> bool:
    """Soft-delete a webhook. Returns True if deleted."""
    wh = Webhook.query.filter_by(id=webhook_id, agent_id=agent_id).first()
    if not wh:
        return False
    wh.active = False
    db.session.commit()
    return True


def fire_event(event: str, task_id: str, data: dict):
    """Fire a webhook event to all matching subscribers (non-blocking)."""
    from models import Job
    job = Job.query.get(task_id)
    if not job:
        return

    # Collect agent IDs who might care about this event
    agent_ids = set()
    if job.buyer_id:
        agent_ids.add(job.buyer_id)
    # G10: Use JobParticipant join table instead of deprecated JSON array
    for jp in JobParticipant.query.filter_by(task_id=task_id, unclaimed_at=None).all():
        agent_ids.add(jp.worker_id)

    if not agent_ids:
        return

    # Find matching webhooks
    webhooks = Webhook.query.filter(
        Webhook.agent_id.in_(agent_ids),
        Webhook.active.is_(True),
    ).all()

    matching = [wh for wh in webhooks if event in (wh.events or [])]
    if not matching:
        return

    payload = {
        "event": event,
        "task_id": task_id,
        "data": data,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    for wh in matching:
        t = threading.Thread(
            target=_deliver_webhook,
            args=(wh.url, wh.secret, payload),
            daemon=True,
        )
        t.start()


def _deliver_webhook(url: str, secret: str, payload: dict):
    """Deliver a webhook with retries and HMAC signature."""
    # M8 fix: Re-validate URL at delivery time to prevent DNS rebinding
    if not is_safe_webhook_url(url):
        logger.warning("Webhook URL %s failed safety re-check at delivery time, skipping", url)
        return

    body = json.dumps(payload, default=str)
    signature = hmac.new(
        secret.encode() if secret else b'',
        body.encode(),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        'Content-Type': 'application/json',
        'X-Webhook-Signature': f'sha256={signature}',
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = http_requests.post(url, data=body, headers=headers, timeout=10)
            if resp.status_code < 400:
                logger.info("Webhook delivered to %s (status %d)", url, resp.status_code)
                return
            logger.warning("Webhook %s returned %d (attempt %d/%d)",
                           url, resp.status_code, attempt + 1, MAX_RETRIES)
        except Exception as e:
            logger.warning("Webhook delivery to %s failed (attempt %d/%d): %s",
                           url, attempt + 1, MAX_RETRIES, e)

        if attempt < MAX_RETRIES - 1:
            time.sleep(BACKOFF_BASE * (2 ** attempt))

    logger.error("Webhook delivery to %s exhausted all retries", url)


def _to_dict(wh: Webhook, include_secret: bool = False) -> dict:
    d = {
        "webhook_id": wh.id,
        "agent_id": wh.agent_id,
        "url": wh.url,
        "events": wh.events or [],
        "active": wh.active,
        "created_at": wh.created_at.isoformat() if wh.created_at else None,
    }
    if include_secret:
        d["secret"] = wh.secret
    return d
