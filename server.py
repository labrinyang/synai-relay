"""
SYNAI Relay Protocol — V2 Server
Flask application implementing the V2 multi-worker oracle architecture.

Job statuses:  open -> funded -> resolved | expired | cancelled
Submission statuses: pending -> judging -> passed | failed
"""

from flask import Flask, request, jsonify, g
from models import db, Owner, Agent, Job, Submission, Webhook
from config import Config
from sqlalchemy.exc import IntegrityError
from services.auth_service import generate_api_key, require_auth, require_buyer

import json
import logging
import os
import threading
import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Structured logging setup (G14)
# ---------------------------------------------------------------------------

_log_handler = logging.StreamHandler()
_log_handler.setFormatter(logging.Formatter(
    '{"timestamp":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger('relay')

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------

logger.info("Starting SYNAI Relay Protocol V2")
if Config.DEV_MODE:
    logger.warning("⚠️  DEV_MODE ENABLED — chain verification disabled, accepting any tx_hash")

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

# G23: Dev mode response header
if Config.DEV_MODE:
    @app.after_request
    def _add_dev_mode_header(response):
        response.headers['X-Dev-Mode'] = 'true'
        return response

# Thread pool for oracle evaluations with timeout support (G07)
_oracle_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='oracle')

# ---------------------------------------------------------------------------
# Oracle background thread
# ---------------------------------------------------------------------------


def _run_oracle(app, submission_id):
    """Background thread: guard check + 6-step oracle evaluation."""
    with app.app_context():
        sub = Submission.query.get(submission_id)
        if not sub or sub.status != 'judging':
            return

        try:
            job = Job.query.get(sub.task_id)
            if not job:
                return

            # Step 1: Guard
            from services.oracle_guard import OracleGuard
            guard = OracleGuard()
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

            sub.oracle_score = result['score']
            sub.oracle_reason = result['reason']
            sub.oracle_steps = (
                [{"step": 1, "name": "guard", "output": guard_result}] + result['steps']
            )

            if result['verdict'] == 'RESOLVED':
                sub.status = 'passed'

                # Atomic resolve: only the first passer wins
                updated = Job.query.filter_by(
                    task_id=sub.task_id, status='funded'
                ).update({
                    'status': 'resolved',
                    'winner_id': sub.worker_id,
                    'result_data': sub.content,
                })

                if updated:
                    # H7: Discard other in-flight submissions
                    Submission.query.filter(
                        Submission.task_id == sub.task_id,
                        Submission.id != sub.id,
                        Submission.status.in_(['pending', 'judging']),
                    ).update({'status': 'failed'}, synchronize_session='fetch')

                    # This submission won — attempt payout (G06: track status)
                    job_obj = Job.query.get(sub.task_id)
                    worker = Agent.query.get(sub.worker_id)
                    if worker and worker.wallet_address:
                        from services.wallet_service import get_wallet_service
                        wallet = get_wallet_service()
                        if wallet.is_connected():
                            job_obj.payout_status = 'pending'
                            db.session.flush()
                            try:
                                # G19: Use per-job fee_bps
                                fee_bps = job_obj.fee_bps or Config.PLATFORM_FEE_BPS
                                txs = wallet.payout(worker.wallet_address, job.price, fee_bps=fee_bps)
                                job_obj.payout_tx_hash = txs['payout_tx']
                                job_obj.fee_tx_hash = txs.get('fee_tx')
                                job_obj.payout_status = 'success'
                                worker_share = Decimal(10000 - fee_bps) / Decimal(10000)
                                worker.total_earned = (
                                    (worker.total_earned or 0) + job.price * worker_share
                                )
                            except Exception as e:
                                job_obj.payout_status = 'failed'
                                logger.error("Payout failed for submission %s: %s", sub.id, e)
                        else:
                            job_obj.payout_status = 'skipped'
                    else:
                        if job_obj:
                            job_obj.payout_status = 'skipped'

                    # Update reputation
                    from services.agent_service import AgentService
                    AgentService.update_reputation(sub.worker_id)
            else:
                sub.status = 'failed'
                # Increment failure count
                job_obj = Job.query.get(sub.task_id)
                if job_obj:
                    job_obj.failure_count = (job_obj.failure_count or 0) + 1

                # Update reputation on failure too
                from services.agent_service import AgentService
                AgentService.update_reputation(sub.worker_id)

            db.session.commit()
        except Exception as e:
            sub.status = 'failed'
            # M8: Don't leak internal error details to client
            sub.oracle_reason = "Internal processing error"
            sub.oracle_steps = [{"step": 0, "name": "error", "output": {"error": "internal"}}]
            logger.exception("Oracle exception for submission %s", sub.id)
            db.session.commit()


def _launch_oracle_with_timeout(submission_id):
    """Submit oracle evaluation to thread pool with timeout (G07)."""
    timeout = Config.ORACLE_TIMEOUT_SECONDS

    def _oracle_with_timeout():
        try:
            future = _oracle_executor.submit(_run_oracle, app, submission_id)
            future.result(timeout=timeout)
        except FuturesTimeoutError:
            # Oracle timed out — mark submission as failed
            with app.app_context():
                sub = Submission.query.get(submission_id)
                if sub and sub.status == 'judging':
                    sub.status = 'failed'
                    sub.oracle_reason = f"Evaluation timed out after {timeout}s"
                    sub.oracle_steps = [{"step": 0, "name": "timeout", "output": {"error": "timeout"}}]
                    db.session.commit()
                    logger.warning("Oracle timeout for submission %s after %ds", submission_id, timeout)
        except Exception as e:
            logger.exception("Oracle launcher error for submission %s", submission_id)

    t = threading.Thread(target=_oracle_with_timeout, daemon=True)
    t.start()


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


def _submission_to_dict(sub: Submission) -> dict:
    return {
        "submission_id": sub.id,
        "task_id": sub.task_id,
        "worker_id": sub.worker_id,
        "content": sub.content,
        "status": sub.status,
        "oracle_score": sub.oracle_score,
        "oracle_reason": sub.oracle_reason,
        "oracle_steps": _sanitize_oracle_steps(sub.oracle_steps),
        "attempt": sub.attempt,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }


# ===================================================================
# 1. GET /health
# ===================================================================


@app.route('/health', methods=['GET'])
def health():
    result = {"status": "healthy", "service": "synai-relay-v2"}
    if Config.DEV_MODE:
        result["dev_mode"] = True
    return jsonify(result), 200


# ===================================================================
# 2. GET /platform/deposit-info
# ===================================================================


@app.route('/platform/deposit-info', methods=['GET'])
def deposit_info():
    from services.wallet_service import get_wallet_service
    wallet = get_wallet_service()
    return jsonify({
        "operations_wallet": wallet.get_ops_address(),
        "usdc_contract": app.config.get('USDC_CONTRACT', ''),
        "chain": "base",
        "min_amount": app.config.get('MIN_TASK_AMOUNT', 0.1),
        "chain_connected": wallet.is_connected(),
    }), 200


# ===================================================================
# 3. POST /agents — register agent
# ===================================================================


@app.route('/agents', methods=['POST'])
def register_agent():
    from services.agent_service import AgentService

    data = request.get_json(silent=True) or {}
    agent_id = data.get('agent_id')
    name = data.get('name')
    wallet_address = data.get('wallet_address')

    if not agent_id:
        return jsonify({"error": "agent_id is required"}), 400

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
    import re

    # Must be the agent themselves
    if g.current_agent_id != agent_id:
        return jsonify({"error": "Cannot update another agent's profile"}), 403

    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    data = request.get_json(silent=True) or {}

    if 'name' in data:
        agent.name = data['name']

    if 'wallet_address' in data:
        wallet = data['wallet_address']
        if wallet and not re.match(r'^0x[0-9a-fA-F]{40}$', wallet):
            return jsonify({"error": "Invalid wallet address format"}), 400
        agent.wallet_address = wallet

    db.session.commit()

    from services.agent_service import AgentService
    return jsonify(AgentService.get_profile(agent_id)), 200


# ===================================================================
# 5 & 6. /jobs — POST create | GET list
# ===================================================================


@app.route('/jobs', methods=['GET'])
def list_jobs_endpoint():
    return _list_jobs()


@app.route('/jobs', methods=['POST'])
@require_auth
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
    if not description:
        return jsonify({"error": "description is required"}), 400

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
    artifact_type = data.get('artifact_type', 'GENERAL')

    expiry = None
    raw_expiry = data.get('expiry')
    if raw_expiry is not None:
        try:
            expiry = datetime.datetime.utcfromtimestamp(int(raw_expiry))
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
        participants=[],
    )

    db.session.add(job)
    db.session.commit()

    return jsonify({
        "status": "open",
        "task_id": job.task_id,
        "price": float(job.price),
    }), 201


def _list_jobs():
    from services.job_service import JobService

    status = request.args.get('status')
    buyer_id = request.args.get('buyer_id')
    worker_id = request.args.get('worker_id')

    jobs = JobService.list_jobs(status=status, buyer_id=buyer_id, worker_id=worker_id)
    return jsonify([JobService.to_dict(j) for j in jobs]), 200


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
    from services.job_service import JobService
    from services.wallet_service import get_wallet_service

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

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
    if wallet.is_connected():
        # On-chain verification
        verify = wallet.verify_deposit(tx_hash, job.price)
        if not verify.get('valid'):
            return jsonify({"error": "Deposit verification failed"}), 400
        job.depositor_address = verify.get('depositor')
    elif not app.config.get('DEV_MODE', False):
        return jsonify({"error": "Chain not connected and DEV_MODE is disabled"}), 503
    # else: dev mode — accept any tx_hash

    job.status = 'funded'
    job.deposit_tx_hash = tx_hash
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "This transaction has already been used to fund a job"}), 409

    return jsonify({
        "status": "funded",
        "task_id": task_id,
        "tx_hash": tx_hash,
    }), 200


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
    participants = list(job.participants or [])
    if worker_id in participants:
        return jsonify({"error": "Worker already claimed this task"}), 409

    # Add to participants
    participants.append(worker_id)
    job.participants = participants
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
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    worker_id = g.current_agent_id

    if job.status not in ('funded',):
        return jsonify({"error": f"Cannot unclaim from job in {job.status} state"}), 400

    participants = list(job.participants or [])
    if worker_id not in participants:
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

    participants.remove(worker_id)
    job.participants = participants
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
    participants = list(job.participants or [])
    if worker_id not in participants:
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

    subs = Submission.query.filter_by(task_id=task_id).order_by(
        Submission.created_at.asc()
    ).all()

    return jsonify([_submission_to_dict(s) for s in subs]), 200


# ===================================================================
# 12. GET /submissions/<submission_id> — get submission details
# ===================================================================


@app.route('/submissions/<submission_id>', methods=['GET'])
def get_submission(submission_id):
    sub = Submission.query.get(submission_id)
    if not sub:
        return jsonify({"error": "Submission not found"}), 404
    return jsonify(_submission_to_dict(sub)), 200


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

        Submission.query.filter(
            Submission.task_id == task_id,
            Submission.status.in_(['pending', 'judging']),
        ).update({'status': 'failed'}, synchronize_session='fetch')

    job.status = 'cancelled'
    db.session.commit()

    return jsonify({
        "status": "cancelled",
        "task_id": task_id,
    }), 200


# ===================================================================
# 14. POST /jobs/<task_id>/refund — refund funded expired/cancelled task
# ===================================================================


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

    # Mark refund as in-progress before sending (prevents concurrent attempts)
    job.refund_tx_hash = 'pending'
    db.session.flush()

    # Attempt on-chain refund if wallet is connected and deposit info exists
    wallet = get_wallet_service()
    refund_tx = None
    if wallet.is_connected() and job.depositor_address and job.deposit_tx_hash:
        try:
            refund_tx = wallet.refund(job.depositor_address, job.price)
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
        "amount": float(job.price),
    }
    if refund_tx:
        result["refund_tx_hash"] = refund_tx
    return jsonify(result), 200


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(port=5005, debug=os.environ.get('FLASK_DEBUG', 'false').lower() in ('true', '1'))
