"""
SYNAI Relay Protocol — V2 Server
Flask application implementing the V2 multi-worker oracle architecture.

Job statuses:  open -> funded -> resolved | expired | cancelled
Submission statuses: pending -> judging -> passed | failed
"""

from flask import Flask, request, jsonify, g, render_template, send_from_directory
from models import db, Owner, Agent, Job, Submission, Webhook, IdempotencyKey, Dispute, JobParticipant
from config import Config
from sqlalchemy.exc import IntegrityError
from services.auth_service import generate_api_key, require_auth, require_operator, require_buyer, verify_api_key
from services.rate_limiter import rate_limit, get_submit_limiter
import re

import atexit
import json
import logging
import os
import threading
import datetime
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Structured logging setup (G14, M7 fix: proper JSON escaping)
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Produce valid JSON log lines even when message contains quotes/newlines."""
    def format(self, record):
        import json as _json
        from flask import has_request_context
        entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if has_request_context():
            from flask import g as _g
            rid = getattr(_g, 'request_id', None)
            if rid:
                entry["request_id"] = rid
        return _json.dumps(entry)


_log_handler = logging.StreamHandler()
_log_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger('relay')

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# Enable WAL mode for SQLite concurrent access (test process + running server)
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine

@sa_event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    import sqlite3
    if isinstance(dbapi_conn, sqlite3.Connection):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------

logger.info("Starting SYNAI Relay Protocol V2")
# C7: Warn about SQLite limitations
if 'sqlite' in Config.SQLALCHEMY_DATABASE_URI:
    logger.warning("SQLite detected — row-level locking (with_for_update) is NOT supported. "
                    "Use PostgreSQL for production deployments.")

# P0-2: Startup guard — reject SQLite in production
Config.validate_production()

# P1-8: Startup check — Guard Layer B configuration
guard_url = os.environ.get('ORACLE_LLM_BASE_URL', '')
guard_key = os.environ.get('ORACLE_LLM_API_KEY', '')
if not guard_url or not guard_key:
    logger.warning("Guard Layer B not configured. "
                   "Set ORACLE_LLM_BASE_URL and ORACLE_LLM_API_KEY for oracle evaluation.")

with app.app_context():
    try:
        db.create_all()
        logger.info("Database tables created / verified")
    except Exception as e:
        logger.critical("Database init failed: %s", e)

    # L11: Recover stuck judging submissions from previous crash
    try:
        stuck = Submission.query.filter_by(status='judging').count()
        if stuck > 0:
            Submission.query.filter_by(status='judging').update(
                {'status': 'failed', 'oracle_reason': 'Server restarted during evaluation'},
                synchronize_session='fetch'
            )
            db.session.commit()
            logger.info("Recovered %d stuck judging submissions", stuck)
    except Exception as e:
        logger.error("Crash recovery check failed: %s", e)

    # P2-2: Provide app reference for webhook failure tracking in background threads
    from services.webhook_service import set_app_ref
    set_app_ref(app)

# G14: Correlation ID — attach unique request ID to every request
@app.before_request
def _attach_request_id():
    import uuid as _uuid
    rid = request.headers.get('X-Request-ID') or str(_uuid.uuid4())
    g.request_id = rid

@app.after_request
def _add_request_id_header(response):
    rid = getattr(g, 'request_id', None)
    if rid:
        response.headers['X-Request-ID'] = rid
    return response

# Thread pool for oracle evaluations with timeout support (G07)
class _ScheduledExecutor:
    """G18: Wrapper around ThreadPoolExecutor with error recovery and dead-letter logging."""
    def __init__(self, max_workers=4):
        self._max_workers = max_workers
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='oracle')
        self._dead_letters = []  # Recent failures for monitoring
        self._lock = threading.Lock()

    def ensure_pool(self):
        """Recreate pool if needed (for test re-use after teardown)."""
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix='oracle',
            )

    def submit(self, fn, *args, **kwargs):
        self.ensure_pool()
        return self._pool.submit(fn, *args, **kwargs)

    def record_failure(self, submission_id: str, error: str):
        with self._lock:
            self._dead_letters.append({
                "submission_id": submission_id,
                "error": error,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            # Keep only last 100 failures
            if len(self._dead_letters) > 100:
                self._dead_letters = self._dead_letters[-100:]

    def shutdown(self, wait=True):
        """Shutdown the executor."""
        if self._pool:
            self._pool.shutdown(wait=wait)
            self._pool = None

    @property
    def dead_letters(self):
        with self._lock:
            return list(self._dead_letters)

_oracle_executor = _ScheduledExecutor(max_workers=4)

# Graceful shutdown signal for background threads
_shutdown_event = threading.Event()

# Wire shutdown event to webhook service so delivery threads can check it
from services.webhook_service import set_shutdown_event
set_shutdown_event(_shutdown_event)

import time as _time_mod  # needed for monotonic()
_pending_oracles = {}  # submission_id -> (future, start_time, timeout_seconds)
_pending_lock = threading.Lock()


# ---------------------------------------------------------------------------
# G12: Proactive expiry checker (background loop)
# ---------------------------------------------------------------------------

def _expiry_checker_loop():
    """Background thread: check for expired funded jobs every 60 seconds."""
    import time
    consecutive_errors = 0
    while not _shutdown_event.is_set():
        try:
            # m6 fix: Exponential backoff on consecutive errors (60s, 120s, 240s, max 600s)
            sleep_time = min(60 * (2 ** consecutive_errors), 600)
            if _shutdown_event.wait(timeout=sleep_time):
                break  # Shutdown requested during sleep
            with app.app_context():
                try:
                    from services.job_service import JobService
                    now = datetime.datetime.now(datetime.timezone.utc)
                    expired_jobs = Job.query.filter(
                        Job.status == 'funded',
                        Job.expiry.isnot(None),
                        Job.expiry < now,
                    ).all()
                    for job in expired_jobs:
                        if JobService.check_expiry(job):
                            logger.info("Proactively expired job %s", job.task_id)
                            from services.webhook_service import fire_event
                            fire_event('job.expired', job.task_id, {"status": "expired"})
                    if expired_jobs:
                        db.session.commit()

                    # Clean up expired idempotency keys (24h TTL)
                    from models import IdempotencyKey
                    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
                    deleted = IdempotencyKey.query.filter(
                        IdempotencyKey.created_at < cutoff
                    ).delete(synchronize_session=False)
                    if deleted:
                        db.session.commit()
                        logger.info("Cleaned up %d expired idempotency keys", deleted)
                finally:
                    db.session.remove()
            consecutive_errors = 0  # Reset on success
        except Exception as e:
            consecutive_errors += 1
            logger.error("Expiry checker error (consecutive=%d): %s", consecutive_errors, e)

_expiry_thread = threading.Thread(target=_expiry_checker_loop, daemon=True, name='expiry-checker')
_expiry_thread.start()


# ---------------------------------------------------------------------------
# G07: Timeout monitor — single background thread replaces per-submission wrappers
# ---------------------------------------------------------------------------

def _mark_submission_timed_out(submission_id):
    """Mark a submission as timed out. Called by timeout monitor."""
    timeout = Config.ORACLE_TIMEOUT_SECONDS
    with app.app_context():
        try:
            sub = db.session.query(Submission).filter_by(
                id=submission_id
            ).with_for_update().first()
            if sub and sub.status == 'judging':
                sub.status = 'failed'
                sub.oracle_reason = f"Evaluation timed out after {timeout}s"
                sub.oracle_steps = [{"step": 0, "name": "timeout", "output": {"error": "timeout"}}]
                db.session.commit()
                logger.warning("Oracle timeout for submission %s after %ds", submission_id, timeout)
        except Exception as e:
            logger.error("Error marking submission %s as timed out: %s", submission_id, e)
        finally:
            db.session.remove()
    _oracle_executor.record_failure(submission_id, f"timeout after {timeout}s")


def _timeout_monitor_loop():
    """Background thread: check for timed-out oracle evaluations every 5 seconds."""
    while not _shutdown_event.is_set():
        if _shutdown_event.wait(timeout=5):
            break
        with _pending_lock:
            now = _time_mod.monotonic()
            expired = [
                (sid, fut) for sid, (fut, start, tout) in _pending_oracles.items()
                if now - start > tout
            ]
        for sid, fut in expired:
            fut.cancel()  # best-effort cancel
            _mark_submission_timed_out(sid)
            with _pending_lock:
                _pending_oracles.pop(sid, None)
        # Clean up completed futures
        with _pending_lock:
            done = [sid for sid, (fut, _, _) in _pending_oracles.items() if fut.done()]
            for sid in done:
                _pending_oracles.pop(sid)


_timeout_monitor = threading.Thread(target=_timeout_monitor_loop, daemon=True, name='oracle-timeout-monitor')
_timeout_monitor.start()


def _atexit_shutdown():
    """Graceful cleanup: signal daemon threads to stop and drain pools."""
    _shutdown_event.set()
    _oracle_executor.shutdown(wait=False)
    try:
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
    except Exception:
        pass

atexit.register(_atexit_shutdown)

# ---------------------------------------------------------------------------
# Oracle background thread
# ---------------------------------------------------------------------------


def _run_oracle(app, submission_id):
    """Background thread: guard check + 6-step oracle evaluation."""
    if _shutdown_event.is_set():
        return
    with app.app_context():
        try:
            # C1 fix: Re-read with lock to prevent race with timeout handler
            sub = db.session.query(Submission).filter_by(id=submission_id).with_for_update().first()
            if not sub or sub.status != 'judging':
                return
            job = db.session.get(Job, sub.task_id)
            if not job:
                return

            # Step 1: Guard
            from services.oracle_guard import OracleGuard
            guard = OracleGuard()

            # P1-2 fix (M-O03): Guard scans rubric and description for injection
            if job.rubric:
                rubric_guard = guard.check_rubric(job.rubric)
                if rubric_guard['blocked']:
                    sub.status = 'failed'
                    sub.oracle_score = 0
                    sub.oracle_reason = f"Blocked by guard (rubric injection): {rubric_guard['reason']}"
                    sub.oracle_steps = [{"step": 1, "name": "guard_rubric", "output": rubric_guard}]
                    db.session.commit()
                    return

            if job.description:
                desc_guard = guard.check_rubric(job.description)
                if desc_guard['blocked']:
                    sub.status = 'failed'
                    sub.oracle_score = 0
                    sub.oracle_reason = f"Blocked by guard (description injection): {desc_guard['reason']}"
                    sub.oracle_steps = [{"step": 1, "name": "guard_description", "output": desc_guard}]
                    db.session.commit()
                    return

            text_for_guard = (
                json.dumps(sub.content, ensure_ascii=False)
                if isinstance(sub.content, dict)
                else str(sub.content)
            )
            guard_result = guard.check(text_for_guard)

            if guard_result['blocked']:
                sub.status = 'failed'
                sub.oracle_score = 0
                sub.oracle_reason = f"Blocked by guard: {guard_result['reason']}"
                sub.oracle_steps = [{"step": 1, "name": "guard", "output": guard_result}]
                db.session.commit()
                return

            # Steps 2-6: Oracle evaluation
            from services.oracle_service import OracleService
            oracle = OracleService()
            result = oracle.evaluate(job.title, job.description, job.rubric, sub.content)

            # Don't write results during shutdown
            if _shutdown_event.is_set():
                return

            # C1 fix: Re-check status under lock before writing results
            # (timeout handler may have set status='failed' while we were evaluating)
            sub = db.session.query(Submission).filter_by(id=submission_id).with_for_update().first()
            if not sub or sub.status != 'judging':
                return  # Timeout handler already marked this submission

            sub.oracle_score = result['score']
            sub.oracle_reason = result['reason']
            sub.oracle_steps = (
                [{"step": 1, "name": "guard", "output": guard_result}] + result['steps']
            )

            if result['verdict'] == 'RESOLVED':
                # C4 fix: Atomic resolve FIRST, then mark submission
                updated = Job.query.filter_by(
                    task_id=sub.task_id, status='funded'
                ).update({
                    'status': 'resolved',
                    'winner_id': sub.worker_id,
                    'result_data': sub.content,
                })

                if updated:
                    sub.status = 'passed'
                    # H7: Discard other in-flight submissions
                    Submission.query.filter(
                        Submission.task_id == sub.task_id,
                        Submission.id != sub.id,
                        Submission.status.in_(['pending', 'judging']),
                    ).update({'status': 'failed'}, synchronize_session='fetch')

                    # This submission won — attempt payout (G06: track status)
                    # P0-3 fix (C-06): Lock Job row before payout to prevent race with cancel
                    job_obj = db.session.query(Job).filter_by(
                        task_id=sub.task_id
                    ).with_for_update().first()

                    if not job_obj or job_obj.status != 'resolved':
                        # State changed concurrently (e.g., cancelled), abort payout
                        sub.status = 'failed'
                        sub.oracle_reason = "Job state changed during payout preparation"
                        db.session.commit()
                        return

                    worker = db.session.get(Agent, sub.worker_id)
                    if worker and worker.wallet_address:
                        from services.wallet_service import get_wallet_service
                        wallet = get_wallet_service()
                        if wallet.is_connected():
                            job_obj.payout_status = 'pending'
                            db.session.flush()
                            try:
                                # G19: Use per-job fee_bps
                                fee_bps = job_obj.fee_bps if job_obj.fee_bps is not None else Config.PLATFORM_FEE_BPS
                                txs = wallet.payout(worker.wallet_address, job_obj.price, fee_bps=fee_bps)
                                job_obj.payout_tx_hash = txs['payout_tx']
                                job_obj.fee_tx_hash = txs.get('fee_tx')

                                # P0-1 fix (C-03): Check pending status
                                if txs.get('pending'):
                                    job_obj.payout_status = 'pending_confirmation'
                                    logger.warning(
                                        "Payout pending confirmation for job %s: %s",
                                        sub.task_id, txs.get('error', 'receipt timeout')
                                    )
                                # P0-1 fix (C-02): Check fee_error
                                elif txs.get('fee_error'):
                                    job_obj.payout_status = 'partial'
                                    logger.error(
                                        "Partial settlement for job %s: worker paid, fee failed: %s",
                                        sub.task_id, txs['fee_error']
                                    )
                                else:
                                    job_obj.payout_status = 'success'

                                # Only count worker earnings when not pending
                                if not txs.get('pending'):
                                    worker_share = Decimal(10000 - fee_bps) / Decimal(10000)
                                    worker.total_earned = (
                                        (worker.total_earned or 0) + job_obj.price * worker_share
                                    )
                            except Exception as e:
                                job_obj.payout_status = 'failed'
                                logger.error("Payout failed for submission %s: %s", sub.id, e)
                        else:
                            job_obj.payout_status = 'skipped'
                    else:
                        if job_obj:
                            job_obj.payout_status = 'skipped'

                    # G04: Fire webhook for resolve
                    from services.webhook_service import fire_event
                    fire_event('job.resolved', sub.task_id, {
                        "status": "resolved",
                        "winner_id": sub.worker_id,
                        "score": result['score'],
                    })

                    # Update reputation
                    from services.agent_service import AgentService
                    AgentService.update_reputation(sub.worker_id)
                else:
                    # C4: Job was no longer funded (cancelled/expired during evaluation)
                    sub.status = 'failed'
                    sub.oracle_reason = "Job was no longer in funded state"
            else:
                sub.status = 'failed'
                # Increment failure count
                job_obj = db.session.get(Job, sub.task_id)
                if job_obj:
                    job_obj.failure_count = (job_obj.failure_count or 0) + 1

                # Update reputation on failure too
                from services.agent_service import AgentService
                AgentService.update_reputation(sub.worker_id)

            db.session.commit()

            # G04: Fire webhook for submission result
            from services.webhook_service import fire_event
            fire_event('submission.completed', sub.task_id, {
                "submission_id": sub.id,
                "worker_id": sub.worker_id,
                "status": sub.status,
                "score": sub.oracle_score,
            })
        except Exception as e:
            sub.status = 'failed'
            # M8: Don't leak internal error details to client
            sub.oracle_reason = "Internal processing error"
            sub.oracle_steps = [{"step": 0, "name": "error", "output": {"error": "internal"}}]
            logger.exception("Oracle exception for submission %s", sub.id)
            _oracle_executor.record_failure(submission_id, str(e))
            db.session.commit()
        finally:
            db.session.remove()


def _launch_oracle_with_timeout(submission_id):
    """Submit oracle evaluation to thread pool with timeout tracking (G07)."""
    # F08: Prevent unbounded queue
    _oracle_executor.ensure_pool()
    pending = _oracle_executor._pool._work_queue.qsize() if _oracle_executor._pool else 0
    if pending >= 20:
        logger.warning("Oracle queue saturated (%d pending), rejecting submission %s", pending, submission_id)
        with app.app_context():
            try:
                sub = db.session.query(Submission).filter_by(id=submission_id).first()
                if sub and sub.status == 'judging':
                    sub.status = 'failed'
                    sub.oracle_reason = "Oracle evaluation queue full — please retry later"
                    db.session.commit()
            finally:
                db.session.remove()
        _oracle_executor.record_failure(submission_id, "queue saturated")
        return

    future = _oracle_executor.submit(_run_oracle, app, submission_id)
    with _pending_lock:
        _pending_oracles[submission_id] = (future, _time_mod.monotonic(), Config.ORACLE_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# G17: Idempotency key helper
# ---------------------------------------------------------------------------


def check_idempotency():
    """Check for Idempotency-Key header. Returns cached response if found, else None.
    M5: Respects 24h TTL. M10: Requires authenticated agent (no 'anon' fallback)."""
    idem_key = request.headers.get('Idempotency-Key')
    if not idem_key:
        return None
    agent_id = getattr(g, 'current_agent_id', None)
    if not agent_id:
        return None  # M10: Skip idempotency for unauthenticated requests
    full_key = f"{agent_id}:{idem_key}"
    cached = db.session.get(IdempotencyKey, full_key)
    if cached:
        if cached.is_expired:
            # M5: Expired — remove and allow re-use
            db.session.delete(cached)
            db.session.commit()
            return None
        return jsonify(cached.response_body), cached.response_code
    return None


def save_idempotency(response_tuple):
    """Save response for the current idempotency key."""
    idem_key = request.headers.get('Idempotency-Key')
    if not idem_key:
        return
    agent_id = getattr(g, 'current_agent_id', None)
    if not agent_id:
        return
    full_key = f"{agent_id}:{idem_key}"
    body, code = response_tuple
    try:
        entry = IdempotencyKey(
            key=full_key,
            agent_id=agent_id,
            response_code=code,
            response_body=body.get_json(),
        )
        db.session.add(entry)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


# ---------------------------------------------------------------------------
# Helper: optional auth viewer extraction (G16)
# ---------------------------------------------------------------------------


def _get_viewer_id() -> str:
    """Extract agent_id from Bearer token if present, without requiring auth."""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
        agent = verify_api_key(token)
        if agent:
            return agent.agent_id
    return None


# ---------------------------------------------------------------------------
# Helper: submission serialiser
# ---------------------------------------------------------------------------


def _sanitize_oracle_steps(steps):
    """Return only step name + pass/fail, hide full LLM outputs."""
    if not steps:
        return steps
    sanitized = []
    for s in steps:
        output = s.get("output")
        if isinstance(output, dict):
            verdict = output.get("verdict")
            if verdict:
                # Explicit verdict exists — use it
                passed = verdict not in ("CLEAR_FAIL", "REJECTED", "BLOCKED")
            else:
                # No verdict field (e.g., guard step uses "blocked" key)
                blocked = output.get("blocked")
                if blocked is not None:
                    passed = not blocked
                else:
                    passed = None
        else:
            passed = None
        sanitized.append({
            "step": s.get("step"),
            "name": s.get("name"),
            "passed": passed,
        })
    return sanitized


def _submission_to_dict(sub: Submission, viewer_id: str = None) -> dict:
    """Serialize submission. Content is only shown to the submitting worker or the job buyer (G16)."""
    # Determine if viewer is allowed to see content
    show_content = False
    if viewer_id:
        if viewer_id == sub.worker_id:
            show_content = True
        else:
            job = db.session.get(Job, sub.task_id)
            if job and viewer_id == job.buyer_id:
                show_content = True

    result = {
        "submission_id": sub.id,
        "task_id": sub.task_id,
        "worker_id": sub.worker_id,
        "status": sub.status,
        "oracle_score": sub.oracle_score,
        "oracle_reason": sub.oracle_reason,
        "oracle_steps": _sanitize_oracle_steps(sub.oracle_steps),
        "attempt": sub.attempt,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }
    if show_content:
        result["content"] = sub.content
    else:
        result["content"] = "[redacted]"
    return result


# ===================================================================
# 1. GET /health
# ===================================================================


@app.route('/health', methods=['GET'])
def health():
    result = {"status": "healthy", "service": "synai-relay-v2"}
    return jsonify(result), 200


# ===================================================================
# 2. GET /platform/deposit-info
# ===================================================================


@app.route('/platform/deposit-info', methods=['GET'])
def deposit_info():
    from services.wallet_service import get_wallet_service
    wallet = get_wallet_service()

    resp = {
        "operations_wallet": wallet.get_ops_address(),
        "usdc_contract": app.config.get('USDC_CONTRACT', ''),
        "chain": "base",
        "chain_id": 8453,
        "min_amount": app.config.get('MIN_TASK_AMOUNT', 0.1),
        "chain_connected": wallet.is_connected(),
        "gas_estimate": None,
    }

    # Provide real-time gas estimation for Buyer/Worker
    if wallet.is_connected():
        from decimal import Decimal
        gas_info = wallet.estimate_gas(wallet.get_ops_address(), Decimal('1.0'))
        if 'error' not in gas_info:
            resp["gas_estimate"] = {
                "gas_limit": gas_info["gas_limit"],
                "gas_price_gwei": gas_info["gas_price_gwei"],
                "estimated_cost_eth": gas_info["estimated_cost_eth"],
                "note": "Real-time estimate for a USDC transfer on Base L2. "
                        "Fetch latest before sending your deposit transaction.",
            }

    return jsonify(resp), 200


# ===================================================================
# 3. POST /agents — register agent
# ===================================================================


@app.route('/agents', methods=['POST'])
@rate_limit()  # C2: Rate limit registration to prevent DoS
def register_agent():
    from services.agent_service import AgentService

    data = request.get_json(silent=True) or {}
    agent_id = data.get('agent_id')
    name = data.get('name')
    wallet_address = data.get('wallet_address')

    if not agent_id:
        return jsonify({"error": "agent_id is required"}), 400

    # C2: Validate agent_id format
    if not re.match(r'^[a-zA-Z0-9_-]{3,100}$', agent_id):
        return jsonify({"error": "agent_id must be 3-100 alphanumeric/hyphen/underscore characters"}), 400

    # M6: Validate wallet before calling service
    if wallet_address and not re.match(r'^0x[0-9a-fA-F]{40}$', wallet_address):
        return jsonify({"error": "Invalid wallet address format"}), 400

    result = AgentService.register(agent_id, name, wallet_address)

    # AgentService.register returns a dict with "error" key if already exists
    if "error" in result:
        return jsonify(result), 409

    # G01: Generate API key and store hash
    raw_key, key_hash = generate_api_key()
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if agent:
        agent.api_key_hash = key_hash
        db.session.commit()

    response = {"status": "registered", "api_key": raw_key, **result}

    # G20: Wallet warning
    if not wallet_address:
        response["warnings"] = ["wallet_address not set — payouts will be skipped"]

    return jsonify(response), 201


# ===================================================================
# 4. GET /agents/<agent_id> — get profile
# ===================================================================


@app.route('/agents/<agent_id>', methods=['GET'])
def get_agent(agent_id):
    from services.agent_service import AgentService

    profile = AgentService.get_profile(agent_id)
    if not profile:
        return jsonify({"error": "Agent not found"}), 404
    return jsonify(profile), 200


# ===================================================================
# 4b. PATCH /agents/<agent_id> — update profile (G02)
# ===================================================================


@app.route('/agents/<agent_id>', methods=['PATCH'])
@require_auth
def update_agent(agent_id):
    # Must be the agent themselves
    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot update another agent's profile"}), 403

    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    data = request.get_json(silent=True) or {}

    if 'name' in data:
        name = data['name']
        if not isinstance(name, str) or len(name) < 1 or len(name) > 200:
            return jsonify({"error": "name must be 1-200 characters"}), 400
        agent.name = name

    if 'wallet_address' in data:
        wallet = data['wallet_address']
        if wallet and not re.match(r'^0x[0-9a-fA-F]{40}$', wallet):
            return jsonify({"error": "Invalid wallet address format"}), 400
        agent.wallet_address = wallet

    db.session.commit()

    from services.agent_service import AgentService
    return jsonify(AgentService.get_profile(agent_id)), 200


# ===================================================================
# 4c. POST /agents/<agent_id>/rotate-key — rotate API key (P2-3)
# ===================================================================


@app.route('/agents/<agent_id>/rotate-key', methods=['POST'])
@require_auth
def rotate_api_key(agent_id):
    """P2-3: Rotate API key for an agent."""
    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot rotate another agent's API key"}), 403

    from services.agent_service import AgentService
    result = AgentService.rotate_api_key(agent_id)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result), 200


# ===================================================================
# 5 & 6. /jobs — POST create | GET list
# ===================================================================


@app.route('/jobs', methods=['GET'])
def list_jobs_endpoint():
    return _list_jobs()


@app.route('/jobs', methods=['POST'])
@require_auth
@rate_limit()
def create_job_endpoint():
    return _create_job()


def _create_job():
    data = request.get_json(silent=True) or {}

    # buyer_id is the authenticated agent
    buyer_id = g.current_agent_id

    # Required fields
    title = data.get('title')
    description = data.get('description')

    if not title:
        return jsonify({"error": "title is required"}), 400
    if len(title) > 500:
        return jsonify({"error": "title must be <= 500 characters"}), 400
    if not description:
        return jsonify({"error": "description is required"}), 400
    if len(description) > 50000:
        return jsonify({"error": "description must be <= 50000 characters"}), 400

    # Price validation
    raw_price = data.get('price')
    if raw_price is None:
        return jsonify({"error": "price is required"}), 400
    try:
        price = Decimal(str(raw_price))
        if not price.is_finite() or price < Decimal(str(Config.MIN_TASK_AMOUNT)):
            return jsonify({
                "error": f"price must be >= {Config.MIN_TASK_AMOUNT}"
            }), 400
    except (InvalidOperation, ValueError, TypeError):
        return jsonify({"error": "Invalid price value"}), 400

    # Optional fields
    rubric = data.get('rubric')
    # P2-5 fix (m-S07): Rubric length limit
    if rubric and len(rubric) > 10000:
        return jsonify({"error": "rubric must be <= 10000 characters"}), 400
    artifact_type = data.get('artifact_type', 'GENERAL')

    expiry = None
    raw_expiry = data.get('expiry')
    if raw_expiry is not None:
        try:
            expiry = datetime.datetime.fromtimestamp(int(raw_expiry), tz=datetime.timezone.utc)
        except (ValueError, TypeError, OSError):
            return jsonify({"error": "Invalid expiry timestamp"}), 400

    max_submissions = data.get('max_submissions', 20)
    if not isinstance(max_submissions, int) or max_submissions < 1:
        max_submissions = 20

    max_retries = data.get('max_retries', 3)
    if not isinstance(max_retries, int) or max_retries < 1:
        max_retries = 3

    min_reputation = None
    raw_min_rep = data.get('min_reputation')
    if raw_min_rep is not None:
        try:
            min_reputation = Decimal(str(raw_min_rep))
        except (InvalidOperation, ValueError, TypeError):
            return jsonify({"error": "Invalid min_reputation value"}), 400

    # G19: Per-job fee configuration
    fee_bps = data.get('fee_bps')
    if fee_bps is not None:
        try:
            fee_bps = int(fee_bps)
            if fee_bps < 0 or fee_bps > 10000:
                return jsonify({"error": "fee_bps must be 0-10000"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid fee_bps value"}), 400

    job = Job(
        title=title,
        description=description,
        rubric=rubric,
        price=price,
        buyer_id=buyer_id,
        status='open',
        artifact_type=artifact_type,
        expiry=expiry,
        max_submissions=max_submissions,
        max_retries=max_retries,
        min_reputation=min_reputation,
        fee_bps=fee_bps if fee_bps is not None else Config.PLATFORM_FEE_BPS,
    )

    db.session.add(job)
    db.session.commit()

    return jsonify({
        "status": "open",
        "task_id": job.task_id,
        "price": float(job.price),
    }), 201


def _list_jobs():
    """G03: Enhanced job listing with filtering, sorting, pagination."""
    from services.job_service import JobService

    status = request.args.get('status')
    buyer_id = request.args.get('buyer_id')
    worker_id = request.args.get('worker_id')
    artifact_type = request.args.get('artifact_type')
    min_price = request.args.get('min_price')
    max_price = request.args.get('max_price')
    _ALLOWED_SORT_FIELDS = {'created_at', 'price', 'expiry'}
    sort_by = request.args.get('sort_by', 'created_at')
    if sort_by not in _ALLOWED_SORT_FIELDS:
        sort_by = 'created_at'
    sort_order = request.args.get('sort_order', 'desc')

    try:
        limit = int(request.args.get('limit', 50))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(request.args.get('offset', 0))
    except (ValueError, TypeError):
        offset = 0

    jobs, total = JobService.list_jobs(
        status=status, buyer_id=buyer_id, worker_id=worker_id,
        artifact_type=artifact_type, min_price=min_price, max_price=max_price,
        sort_by=sort_by, sort_order=sort_order,
        limit=limit, offset=offset,
    )

    return jsonify({
        "jobs": [JobService.to_dict(j) for j in jobs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }), 200


# ===================================================================
# 7. GET /jobs/<task_id> — get job details
# ===================================================================


@app.route('/jobs/<task_id>', methods=['GET'])
def get_job(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(JobService.to_dict(job)), 200


# ===================================================================
# 8. POST /jobs/<task_id>/fund — fund a job
# ===================================================================


@app.route('/jobs/<task_id>/fund', methods=['POST'])
@require_auth
def fund_job(task_id):
    # G17: Idempotency check
    cached = check_idempotency()
    if cached:
        return cached

    from services.job_service import JobService
    from services.wallet_service import get_wallet_service

    # F02: Row lock to prevent double-fund race condition
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    JobService.check_expiry(job)

    if job.status != 'open':
        return jsonify({"error": f"Job not in open state (current: {job.status})"}), 400

    # Auth: must be the buyer
    err = require_buyer(job)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    tx_hash = data.get('tx_hash')
    if not tx_hash:
        return jsonify({"error": "tx_hash is required"}), 400

    wallet = get_wallet_service()
    overpayment = None
    if wallet.is_connected():
        # On-chain verification
        verify = wallet.verify_deposit(tx_hash, job.price)
        if not verify.get('valid'):
            return jsonify({"error": "Deposit verification failed"}), 400
        # P1-4 fix (M-F01): Verify depositor matches buyer's registered wallet
        buyer = Agent.query.filter_by(agent_id=g.current_agent_id).first()
        depositor = verify.get('depositor', '').lower()
        if buyer and buyer.wallet_address:
            if depositor and depositor != buyer.wallet_address.lower():
                return jsonify({
                    "error": "Deposit must come from your registered wallet address",
                    "expected": buyer.wallet_address,
                    "actual": verify.get('depositor'),
                }), 400

        job.depositor_address = verify.get('depositor')
        job.deposit_amount = verify.get('amount')
        overpayment = verify.get('overpayment')  # G22
    else:
        return jsonify({"error": "Chain not connected"}), 503

    job.status = 'funded'
    job.deposit_tx_hash = tx_hash
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "This transaction has already been used to fund a job"}), 409

    resp_data = {
        "status": "funded",
        "task_id": task_id,
        "tx_hash": tx_hash,
    }
    # G22: Report overpayment in response
    if overpayment:
        resp_data["warnings"] = [
            f"Overpayment of {overpayment} USDC detected. "
            f"The full deposited amount will be refunded if the job is cancelled or expires. "
            f"Only the job price ({float(job.price)} USDC) will be used for settlement."
        ]
    result = jsonify(resp_data), 200
    save_idempotency(result)
    return result


# ===================================================================
# 9. POST /jobs/<task_id>/claim — worker claims task
# ===================================================================


@app.route('/jobs/<task_id>/claim', methods=['POST'])
@require_auth
def claim_job(task_id):
    # H6: Atomic claim with DB-level locking
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status != 'funded':
        return jsonify({"error": f"Job not claimable (current status: {job.status})"}), 400

    worker_id = g.current_agent_id

    # Self-dealing prevention
    if worker_id == job.buyer_id:
        return jsonify({"error": "Buyer cannot claim their own task"}), 403

    # Worker must be registered
    worker = Agent.query.filter_by(agent_id=worker_id).first()
    if not worker:
        return jsonify({"error": "Worker not registered. POST /agents first."}), 400

    # Min reputation check
    if job.min_reputation is not None:
        worker_rate = (
            float(worker.completion_rate)
            if worker.completion_rate is not None
            else 0.0
        )
        if worker_rate < float(job.min_reputation):
            return jsonify({
                "error": (
                    f"Worker reputation {worker_rate} "
                    f"below minimum {float(job.min_reputation)}"
                )
            }), 403

    # Check worker not already a participant
    existing = JobParticipant.query.filter_by(task_id=task_id, worker_id=worker_id, unclaimed_at=None).first()
    if existing:
        return jsonify({"error": "Worker already claimed this task"}), 409

    # F04: Check for previously unclaimed record — reactivate instead of creating new
    previously_unclaimed = JobParticipant.query.filter_by(task_id=task_id, worker_id=worker_id).filter(
        JobParticipant.unclaimed_at.isnot(None)
    ).first()
    if previously_unclaimed:
        previously_unclaimed.unclaimed_at = None
        previously_unclaimed.claimed_at = datetime.datetime.now(datetime.timezone.utc)
        db.session.commit()
        result = {"status": "claimed", "task_id": task_id, "worker_id": worker_id}
        if not worker.wallet_address:
            result["warnings"] = ["wallet_address not set — payouts will be skipped"]
        return jsonify(result), 200

    # Add to participants (new claim)
    jp = JobParticipant(task_id=task_id, worker_id=worker_id)
    db.session.add(jp)
    db.session.commit()

    result = {
        "status": "claimed",
        "task_id": task_id,
        "worker_id": worker_id,
    }

    # G20: Wallet warning at claim time
    if not worker.wallet_address:
        result["warnings"] = ["wallet_address not set — payouts will be skipped"]

    return jsonify(result), 200


# ===================================================================
# 9b. POST /jobs/<task_id>/unclaim — worker withdraws (G05)
# ===================================================================


@app.route('/jobs/<task_id>/unclaim', methods=['POST'])
@require_auth
def unclaim_job(task_id):
    # M3 fix: Use row lock to match claim_job pattern
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    worker_id = g.current_agent_id

    if job.status not in ('funded',):
        return jsonify({"error": f"Cannot unclaim from job in {job.status} state"}), 400

    jp = JobParticipant.query.filter_by(task_id=task_id, worker_id=worker_id, unclaimed_at=None).first()
    if not jp:
        return jsonify({"error": "Worker has not claimed this task"}), 400

    # Block unclaim if worker has active judging submissions
    active_judging = Submission.query.filter(
        Submission.task_id == task_id,
        Submission.worker_id == worker_id,
        Submission.status == 'judging',
    ).count()
    if active_judging > 0:
        return jsonify({"error": "Cannot unclaim: submissions are being judged"}), 400

    # Cancel any pending submissions from this worker
    Submission.query.filter(
        Submission.task_id == task_id,
        Submission.worker_id == worker_id,
        Submission.status == 'pending',
    ).update({'status': 'failed'}, synchronize_session='fetch')

    jp.unclaimed_at = datetime.datetime.now(datetime.timezone.utc)
    db.session.commit()

    return jsonify({
        "status": "unclaimed",
        "task_id": task_id,
        "worker_id": worker_id,
    }), 200


# ===================================================================
# 10. POST /jobs/<task_id>/submit — worker submits result
# ===================================================================


@app.route('/jobs/<task_id>/submit', methods=['POST'])
@require_auth
@rate_limit(get_submit_limiter())
def submit_result(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status != 'funded':
        return jsonify({
            "error": f"Job not accepting submissions (current status: {job.status})"
        }), 400

    data = request.get_json(silent=True) or {}
    worker_id = g.current_agent_id
    content = data.get('content')

    if content is None:
        return jsonify({"error": "content is required"}), 400

    # C3: Enforce 50KB content size limit
    content_str = json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else str(content)
    if len(content_str.encode('utf-8')) > 50 * 1024:
        return jsonify({"error": "Submission content exceeds 50KB limit"}), 400

    # Worker must be a participant
    jp = JobParticipant.query.filter_by(task_id=task_id, worker_id=worker_id, unclaimed_at=None).first()
    if not jp:
        return jsonify({"error": "Worker has not claimed this task"}), 403

    # H10: Check submission limits within a locked read
    job_for_submit = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    total_submissions = Submission.query.filter_by(task_id=task_id).count()
    if total_submissions >= (job_for_submit.max_submissions or 20):
        return jsonify({"error": "Task has reached maximum submissions"}), 400

    # Check max_retries per worker
    worker_submissions = Submission.query.filter_by(
        task_id=task_id, worker_id=worker_id
    ).count()
    if worker_submissions >= (job.max_retries or 3):
        return jsonify({"error": "Maximum retries reached for this worker"}), 400

    # Create submission with status 'judging' before starting thread
    attempt_number = worker_submissions + 1
    sub = Submission(
        task_id=task_id,
        worker_id=worker_id,
        content=content,
        status='judging',
        attempt=attempt_number,
    )
    db.session.add(sub)
    db.session.commit()

    # Launch oracle with timeout (G07)
    _launch_oracle_with_timeout(sub.id)

    return jsonify({
        "status": "judging",
        "submission_id": sub.id,
        "attempt": sub.attempt,
    }), 202


# ===================================================================
# 11. GET /jobs/<task_id>/submissions — list submissions for a task
# ===================================================================


@app.route('/jobs/<task_id>/submissions', methods=['GET'])
def list_submissions(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    viewer_id = _get_viewer_id()

    try:
        limit = min(max(1, int(request.args.get('limit', 50))), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        offset = 0

    query = Submission.query.filter_by(task_id=task_id).order_by(
        Submission.created_at.asc()
    )
    total = query.count()
    subs = query.offset(offset).limit(limit).all()

    return jsonify({
        "submissions": [_submission_to_dict(s, viewer_id=viewer_id) for s in subs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }), 200


# ===================================================================
# 12. GET /submissions/<submission_id> — get submission details
# ===================================================================


@app.route('/submissions/<submission_id>', methods=['GET'])
def get_submission(submission_id):
    sub = db.session.get(Submission, submission_id)
    if not sub:
        return jsonify({"error": "Submission not found"}), 404
    viewer_id = _get_viewer_id()
    return jsonify(_submission_to_dict(sub, viewer_id=viewer_id)), 200


# ===================================================================
# 12b. GET /submissions — cross-job submission query (G16)
# ===================================================================


@app.route('/submissions', methods=['GET'])
def list_all_submissions():
    """G16: Cross-job submission query with worker_id filter."""
    worker_id = request.args.get('worker_id')
    if not worker_id:
        return jsonify({"error": "worker_id query parameter is required"}), 400

    viewer_id = _get_viewer_id()

    try:
        limit = min(max(1, int(request.args.get('limit', 50))), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        offset = 0

    query = Submission.query.filter_by(worker_id=worker_id).order_by(
        Submission.created_at.desc()
    )
    total = query.count()
    subs = query.offset(offset).limit(limit).all()

    return jsonify({
        "submissions": [_submission_to_dict(s, viewer_id=viewer_id) for s in subs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }), 200


# ===================================================================
# 13. POST /jobs/<task_id>/cancel — cancel unfunded task
# ===================================================================


@app.route('/jobs/<task_id>/cancel', methods=['POST'])
@require_auth
def cancel_job(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Auth: must be the buyer
    err = require_buyer(job)
    if err:
        return err

    if job.status not in ('open', 'funded'):
        return jsonify({"error": f"Cannot cancel job in {job.status} state"}), 400

    # C2: Lock the job row to prevent cancel/resolve race
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()

    # Re-check status under lock (may have changed concurrently)
    if job.status not in ('open', 'funded'):
        return jsonify({"error": f"Cannot cancel job in {job.status} state"}), 400

    if job.status == 'funded':
        # Block cancel if any submission is actively being judged
        active_judging = Submission.query.filter(
            Submission.task_id == task_id,
            Submission.status == 'judging',
        ).count()
        if active_judging > 0:
            return jsonify({"error": "Cannot cancel: submissions are being judged"}), 409

        # Cancel pending submissions (judging already confirmed == 0 above)
        Submission.query.filter(
            Submission.task_id == task_id,
            Submission.status == 'pending',
        ).update({'status': 'failed'}, synchronize_session='fetch')

    job.status = 'cancelled'

    # P2-7 fix (m-S01): Auto-refund for cancelled funded jobs
    auto_refund_tx = None
    cooldown_blocked, _ = _check_refund_cooldown(job.depositor_address)
    if job.deposit_tx_hash and job.depositor_address and not job.refund_tx_hash and not cooldown_blocked:
        from services.wallet_service import get_wallet_service
        wallet = get_wallet_service()
        if wallet.is_connected():
            try:
                refund_amount = job.deposit_amount if job.deposit_amount is not None else job.price
                auto_refund_tx = wallet.refund(job.depositor_address, refund_amount)
                job.refund_tx_hash = auto_refund_tx
                logger.info("Auto-refund for cancelled job %s: tx=%s", task_id, auto_refund_tx)
            except Exception as e:
                logger.error("Auto-refund failed for job %s: %s (manual refund required)", task_id, e)
    elif cooldown_blocked:
        logger.warning("Auto-refund skipped for job %s: depositor cooldown active", task_id)

    db.session.commit()

    # G04: Fire webhook
    from services.webhook_service import fire_event
    fire_event('job.cancelled', task_id, {"status": "cancelled"})

    result = {"status": "cancelled", "task_id": task_id}
    if auto_refund_tx:
        result["refund_tx_hash"] = auto_refund_tx
        result["refund_status"] = "success"
    elif job.deposit_tx_hash and not job.refund_tx_hash:
        result["refund_status"] = "pending_manual"
        result["message"] = "Automatic refund failed. Use POST /jobs/{task_id}/refund to retry."
    return jsonify(result), 200


# ===================================================================
# 14. POST /jobs/<task_id>/refund — refund funded expired/cancelled task
# ===================================================================


def _check_refund_cooldown(depositor_address):
    """Check if a refund was issued to this address within the last hour.
    Returns (is_blocked, seconds_remaining).
    """
    if not depositor_address:
        return False, 0
    one_hour_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    recent_refund = Job.query.filter(
        Job.depositor_address == depositor_address,
        Job.refund_tx_hash.isnot(None),
        Job.refund_tx_hash != 'pending',
        Job.updated_at >= one_hour_ago,
    ).first()
    if recent_refund:
        updated = recent_refund.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=datetime.timezone.utc)
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - updated).total_seconds()
        remaining = max(0, 3600 - elapsed)
        return True, int(remaining)
    return False, 0


@app.route('/jobs/<task_id>/refund', methods=['POST'])
@require_auth
def refund_job(task_id):
    from services.job_service import JobService
    from services.wallet_service import get_wallet_service

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Auth: must be the buyer
    err = require_buyer(job)
    if err:
        return err

    if job.status not in ('expired', 'cancelled'):
        return jsonify({"error": f"Not refundable in state: {job.status}"}), 400

    # C1: Atomic idempotency check — lock the row to prevent concurrent double-refund
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()

    # Re-check under lock
    if job.status not in ('expired', 'cancelled'):
        return jsonify({"error": f"Not refundable in state: {job.status}"}), 400
    if job.refund_tx_hash:
        return jsonify({"error": "Job already refunded"}), 409

    # Refund cooldown: 1 hour per depositor address
    if job.depositor_address:
        blocked, remaining = _check_refund_cooldown(job.depositor_address)
        if blocked:
            return jsonify({
                "error": "Refund cooldown active for this depositor address",
                "retry_after_seconds": remaining,
            }), 429

    # Mark refund as in-progress before sending (prevents concurrent attempts)
    job.refund_tx_hash = 'pending'
    db.session.flush()

    # Attempt on-chain refund if wallet is connected and deposit info exists
    wallet = get_wallet_service()
    # P1-5 fix (M-F02): Refund actual deposit amount (may include overpayment)
    refund_amount = job.deposit_amount if job.deposit_amount is not None else job.price
    refund_tx = None
    if wallet.is_connected() and job.depositor_address and job.deposit_tx_hash:
        try:
            refund_tx = wallet.refund(job.depositor_address, refund_amount)
            job.refund_tx_hash = refund_tx
        except Exception as e:
            # Rollback the 'pending' marker on failure
            job.refund_tx_hash = None
            db.session.commit()
            logger.error("Refund failed for task %s: %s", task_id, e)
            return jsonify({"error": "Refund processing failed"}), 500

    if not refund_tx:
        # Off-chain mode: mark as refunded without tx
        job.refund_tx_hash = 'off-chain'
    db.session.commit()

    result = {
        "status": "refunded",
        "task_id": task_id,
        "amount": float(refund_amount),
    }
    if refund_tx:
        result["refund_tx_hash"] = refund_tx

    # G04: Fire webhook
    from services.webhook_service import fire_event
    fire_event('job.refunded', task_id, result)

    return jsonify(result), 200


# ===================================================================
# 15. Webhook CRUD — POST/GET/DELETE (G04)
# ===================================================================


@app.route('/agents/<agent_id>/webhooks', methods=['POST'])
@require_auth
def create_webhook(agent_id):
    from services.webhook_service import create_webhook as _create_wh

    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot manage webhooks for another agent"}), 403

    data = request.get_json(silent=True) or {}
    url = data.get('url')
    events = data.get('events', [])

    if not url:
        return jsonify({"error": "url is required"}), 400
    if not url.startswith('https://'):
        return jsonify({"error": "url must use HTTPS"}), 400

    # C1: SSRF protection — validate URL resolves to a public IP
    from services.webhook_service import is_safe_webhook_url
    if not is_safe_webhook_url(url):
        return jsonify({"error": "Webhook URL must resolve to a public IP address"}), 400

    if not isinstance(events, list) or not events:
        return jsonify({"error": "events must be a non-empty list"}), 400

    try:
        result = _create_wh(agent_id, url, events)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result), 201


@app.route('/agents/<agent_id>/webhooks', methods=['GET'])
@require_auth
def list_webhooks(agent_id):
    from services.webhook_service import list_webhooks as _list_wh

    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot view webhooks for another agent"}), 403

    return jsonify(_list_wh(agent_id)), 200


@app.route('/agents/<agent_id>/webhooks/<webhook_id>', methods=['DELETE'])
@require_auth
def delete_webhook(agent_id, webhook_id):
    from services.webhook_service import delete_webhook as _delete_wh

    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot manage webhooks for another agent"}), 403

    if _delete_wh(webhook_id, agent_id):
        return '', 204
    return jsonify({"error": "Webhook not found"}), 404


# ===================================================================
# 16. PATCH /jobs/<task_id> — update job (G11)
# ===================================================================


@app.route('/jobs/<task_id>', methods=['PATCH'])
@require_auth
def update_job(task_id):
    from services.job_service import JobService

    # M4 fix: Lock row to prevent concurrent state changes
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    # Check expiry under lock
    JobService.check_expiry(job)

    err = require_buyer(job)
    if err:
        return err

    data = request.get_json(silent=True) or {}

    if job.status == 'open':
        # P2-5 fix (m-S07): Rubric length limit on update
        if 'rubric' in data and data['rubric'] and len(data['rubric']) > 10000:
            return jsonify({"error": "rubric must be <= 10000 characters"}), 400
        # Mutable when open: title, description, rubric, expiry, max_submissions, max_retries, min_reputation
        for field in ('title', 'description', 'rubric'):
            if field in data:
                setattr(job, field, data[field])
        if 'expiry' in data:
            try:
                job.expiry = datetime.datetime.fromtimestamp(int(data['expiry']), tz=datetime.timezone.utc)
            except (ValueError, TypeError, OSError):
                return jsonify({"error": "Invalid expiry timestamp"}), 400
        for int_field in ('max_submissions', 'max_retries'):
            if int_field in data:
                val = data[int_field]
                if isinstance(val, int) and val >= 1:
                    setattr(job, int_field, val)
        if 'min_reputation' in data:
            try:
                from decimal import Decimal, InvalidOperation
                job.min_reputation = Decimal(str(data['min_reputation']))
            except (InvalidOperation, ValueError, TypeError):
                return jsonify({"error": "Invalid min_reputation"}), 400

    elif job.status == 'funded':
        # When funded: only extend expiry
        if 'expiry' in data:
            try:
                new_expiry = datetime.datetime.fromtimestamp(int(data['expiry']), tz=datetime.timezone.utc)
            except (ValueError, TypeError, OSError):
                return jsonify({"error": "Invalid expiry timestamp"}), 400
            # m7 fix: Must extend existing expiry; if no expiry was set, new one must be >= 24h out
            now = datetime.datetime.now(datetime.timezone.utc)
            if job.expiry and new_expiry <= job.expiry:
                return jsonify({"error": "Can only extend expiry on funded jobs"}), 400
            if not job.expiry and new_expiry < now + datetime.timedelta(hours=24):
                return jsonify({"error": "New expiry on funded job must be at least 24h from now"}), 400
            job.expiry = new_expiry
        else:
            return jsonify({"error": "Only expiry extension allowed on funded jobs"}), 400
    else:
        return jsonify({"error": f"Cannot update job in {job.status} state"}), 400

    db.session.commit()
    return jsonify(JobService.to_dict(job)), 200


# ===================================================================
# 17. GET /platform/solvency — solvency monitoring (G21)
# ===================================================================


@app.route('/platform/solvency', methods=['GET'])
@require_operator  # Operator-only: financial data requires signature verification
def platform_solvency():
    """G21: Solvency overview — outstanding liabilities vs wallet balance."""
    from sqlalchemy import func

    # Outstanding liabilities: funded jobs that haven't been resolved/cancelled
    liabilities = db.session.query(
        func.coalesce(func.sum(Job.price), 0)
    ).filter(Job.status == 'funded').scalar()

    total_payouts = db.session.query(
        func.coalesce(func.sum(Job.price), 0)
    ).filter(
        Job.status == 'resolved',
        Job.payout_status == 'success',
    ).scalar()

    total_refunds = db.session.query(
        func.count(Job.task_id)
    ).filter(
        Job.refund_tx_hash.isnot(None),
        Job.refund_tx_hash != 'pending',
    ).scalar()

    funded_count = Job.query.filter_by(status='funded').count()
    failed_payouts = Job.query.filter_by(payout_status='failed').count()

    return jsonify({
        "outstanding_liabilities": float(liabilities),
        "funded_jobs_count": funded_count,
        "total_payouts_value": float(total_payouts),
        "total_refund_count": total_refunds,
        "failed_payouts_count": failed_payouts,
    }), 200


# ===================================================================
# 17b. POST /admin/jobs/<task_id>/retry-payout (G06)
# ===================================================================


@app.route('/admin/jobs/<task_id>/retry-payout', methods=['POST'])
@require_auth  # F05: buyer/winner check inside function body
def retry_payout(task_id):
    """G06: Retry failed payout for a resolved job."""
    job = db.session.query(Job).filter_by(task_id=task_id).with_for_update().first()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status != 'resolved':
        return jsonify({"error": f"Job is not resolved (current: {job.status})"}), 400

    if job.payout_status != 'failed':
        return jsonify({"error": f"Payout is not in failed state (current: {job.payout_status})"}), 400

    if not job.winner_id:
        return jsonify({"error": "No winner to pay"}), 400

    # F05: Only buyer or winner can retry payout
    if g.current_agent_id not in (job.buyer_id, job.winner_id):
        return jsonify({"error": "Only buyer or winner can retry payout"}), 403

    worker = db.session.get(Agent, job.winner_id)
    if not worker or not worker.wallet_address:
        return jsonify({"error": "Winner has no wallet address"}), 400

    from services.wallet_service import get_wallet_service
    wallet = get_wallet_service()
    if not wallet.is_connected():
        return jsonify({"error": "Chain not connected"}), 503

    try:
        fee_bps = job.fee_bps if job.fee_bps is not None else Config.PLATFORM_FEE_BPS
        txs = wallet.payout(worker.wallet_address, job.price, fee_bps=fee_bps)
        job.payout_tx_hash = txs['payout_tx']
        job.fee_tx_hash = txs.get('fee_tx')

        # P0-1 fix (C-03): Check pending status
        if txs.get('pending'):
            job.payout_status = 'pending_confirmation'
        # P0-1 fix (C-02): Check fee_error
        elif txs.get('fee_error'):
            job.payout_status = 'partial'
        else:
            job.payout_status = 'success'

        # Only count worker earnings when not pending
        if not txs.get('pending'):
            worker_share = Decimal(10000 - fee_bps) / Decimal(10000)
            worker.total_earned = (worker.total_earned or 0) + job.price * worker_share

        db.session.commit()
        return jsonify({
            "status": "payout_retried",
            "task_id": task_id,
            "payout_tx_hash": job.payout_tx_hash,
            "payout_status": job.payout_status,
        }), 200
    except Exception as e:
        db.session.rollback()
        logger.error("Payout retry failed for task %s: %s", task_id, e)
        return jsonify({"error": "Payout retry failed"}), 500


# ===================================================================
# 18. POST /jobs/<task_id>/dispute — dispute stub (G24)
# ===================================================================


@app.route('/jobs/<task_id>/dispute', methods=['POST'])
@require_auth
def dispute_job(task_id):
    """G24: Dispute stub — records dispute request but doesn't resolve it."""
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status != 'resolved':
        return jsonify({"error": f"Can only dispute resolved jobs (current: {job.status})"}), 400

    data = request.get_json(silent=True) or {}
    reason = data.get('reason', '')
    if not reason:
        return jsonify({"error": "reason is required"}), 400

    # Only buyer or winner can dispute
    agent_id = g.current_agent_id
    if agent_id not in (job.buyer_id, job.winner_id):
        return jsonify({"error": "Only buyer or winner can dispute"}), 403

    dispute = Dispute(
        task_id=task_id,
        filed_by=agent_id,
        reason=reason,
    )
    db.session.add(dispute)
    db.session.commit()

    return jsonify({
        "status": "dispute_filed",
        "dispute_id": dispute.id,
        "task_id": task_id,
        "filed_by": agent_id,
        "message": "Dispute recorded. Manual review required.",
    }), 202


# ===================================================================
# Dashboard — read-only HTML pages and stats API
# ===================================================================


@app.route('/')
def landing():
    return render_template('landing.html')


@app.route('/dashboard')
def dashboard_page():
    return render_template('dashboard.html')


@app.route('/skill.md')
def skill_md():
    import pathlib
    if not pathlib.Path(app.root_path, 'static', 'Skill.md').exists():
        return jsonify({"error": "Skill.md not yet available"}), 404
    return send_from_directory('static', 'Skill.md', mimetype='text/markdown')


@app.route('/dashboard/stats', methods=['GET'])
def dashboard_stats():
    from services.dashboard_service import DashboardService, etag_response
    stats = DashboardService.get_stats()
    return etag_response(stats, cache_max_age=30)


@app.route('/dashboard/leaderboard', methods=['GET'])
def dashboard_leaderboard():
    from services.dashboard_service import DashboardService, etag_response
    sort_by = request.args.get('sort_by', 'total_earned')
    if sort_by not in ('total_earned', 'completion_rate'):
        sort_by = 'total_earned'
    try:
        limit = min(max(1, int(request.args.get('limit', 20))), 100)
    except (ValueError, TypeError):
        limit = 20
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        offset = 0
    data = DashboardService.get_leaderboard(sort_by=sort_by, limit=limit, offset=offset)
    return etag_response(data, cache_max_age=30)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(port=5005, debug=os.environ.get('FLASK_DEBUG', 'false').lower() in ('true', '1'))
