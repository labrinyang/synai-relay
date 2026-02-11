"""
SYNAI Relay Protocol — V2 Server
Flask application implementing the V2 multi-worker oracle architecture.

Job statuses:  open -> funded -> resolved | expired | cancelled
Submission statuses: pending -> judging -> passed | failed
"""

from flask import Flask, request, jsonify
from models import db, Owner, Agent, Job, Submission
from config import Config

import json
import threading
import datetime
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------

print("[Relay] Starting SYNAI Relay Protocol V2...")
with app.app_context():
    try:
        db.create_all()
        print("[Relay] Database tables created / verified.")
    except Exception as e:
        print(f"[FATAL] Database init failed: {e}")

# ---------------------------------------------------------------------------
# Oracle background thread
# ---------------------------------------------------------------------------


def _run_oracle(app, submission_id):
    """Background thread: guard check + 6-step oracle evaluation."""
    with app.app_context():
        sub = Submission.query.get(submission_id)
        if not sub or sub.status != 'judging':
            return

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
                # This submission won — attempt payout
                worker = Agent.query.get(sub.worker_id)
                if worker and worker.wallet_address:
                    from services.wallet_service import get_wallet_service
                    wallet = get_wallet_service()
                    if wallet.is_connected():
                        try:
                            txs = wallet.payout(worker.wallet_address, job.price)
                            job_obj = Job.query.get(sub.task_id)
                            job_obj.payout_tx_hash = txs['payout_tx']
                            job_obj.fee_tx_hash = txs['fee_tx']
                            worker.total_earned = (
                                (worker.total_earned or 0) + job.price * Decimal('0.80')
                            )
                        except Exception as e:
                            print(f"[Oracle] Payout failed: {e}")

                # Update reputation
                from services.agent_service import AgentService
                AgentService.update_reputation(sub.worker_id)
        else:
            sub.status = 'failed'
            # Increment failure count
            job_obj = Job.query.get(sub.task_id)
            if job_obj:
                job_obj.failure_count = (job_obj.failure_count or 0) + 1

        db.session.commit()


# ---------------------------------------------------------------------------
# Helper: submission serialiser
# ---------------------------------------------------------------------------


def _submission_to_dict(sub: Submission) -> dict:
    return {
        "submission_id": sub.id,
        "task_id": sub.task_id,
        "worker_id": sub.worker_id,
        "content": sub.content,
        "status": sub.status,
        "oracle_score": sub.oracle_score,
        "oracle_reason": sub.oracle_reason,
        "oracle_steps": sub.oracle_steps,
        "attempt": sub.attempt,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }


# ===================================================================
# 1. GET /health
# ===================================================================


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy", "service": "synai-relay-v2"}), 200


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

    return jsonify({"status": "registered", **result}), 201


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
# 5 & 6. /jobs — POST create | GET list
# ===================================================================


@app.route('/jobs', methods=['GET', 'POST'])
def jobs_endpoint():
    if request.method == 'POST':
        return _create_job()
    return _list_jobs()


def _create_job():
    data = request.get_json(silent=True) or {}

    # Required fields
    title = data.get('title')
    description = data.get('description')
    buyer_id = data.get('buyer_id')

    if not title:
        return jsonify({"error": "title is required"}), 400
    if not description:
        return jsonify({"error": "description is required"}), 400
    if not buyer_id:
        return jsonify({"error": "buyer_id is required"}), 400

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
def fund_job(task_id):
    from services.job_service import JobService
    from services.wallet_service import get_wallet_service

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status != 'open':
        return jsonify({"error": f"Job not in open state (current: {job.status})"}), 400

    data = request.get_json(silent=True) or {}

    buyer_id = data.get('buyer_id')
    if not buyer_id:
        return jsonify({"error": "buyer_id is required"}), 400
    if buyer_id != job.buyer_id:
        return jsonify({"error": "Only the job creator can fund this job"}), 403

    tx_hash = data.get('tx_hash')
    if not tx_hash:
        return jsonify({"error": "tx_hash is required"}), 400

    wallet = get_wallet_service()
    if wallet.is_connected():
        # On-chain verification
        verify = wallet.verify_deposit(tx_hash, job.price)
        if not verify.get('valid'):
            return jsonify({
                "error": f"Deposit verification failed: {verify.get('error', 'unknown')}"
            }), 400
        job.depositor_address = verify.get('depositor')
    # If wallet not connected (dev mode), accept any tx_hash

    job.status = 'funded'
    job.deposit_tx_hash = tx_hash
    db.session.commit()

    return jsonify({
        "status": "funded",
        "task_id": task_id,
        "tx_hash": tx_hash,
    }), 200


# ===================================================================
# 9. POST /jobs/<task_id>/claim — worker claims task
# ===================================================================


@app.route('/jobs/<task_id>/claim', methods=['POST'])
def claim_job(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status != 'funded':
        return jsonify({"error": f"Job not claimable (current status: {job.status})"}), 400

    data = request.get_json(silent=True) or {}
    worker_id = data.get('worker_id')
    if not worker_id:
        return jsonify({"error": "worker_id is required"}), 400

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

    return jsonify({
        "status": "claimed",
        "task_id": task_id,
        "worker_id": worker_id,
    }), 200


# ===================================================================
# 10. POST /jobs/<task_id>/submit — worker submits result
# ===================================================================


@app.route('/jobs/<task_id>/submit', methods=['POST'])
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
    worker_id = data.get('worker_id')
    content = data.get('content')

    if not worker_id:
        return jsonify({"error": "worker_id is required"}), 400
    if content is None:
        return jsonify({"error": "content is required"}), 400

    # Worker must be a participant
    participants = list(job.participants or [])
    if worker_id not in participants:
        return jsonify({"error": "Worker has not claimed this task"}), 403

    # Check max_submissions on the job
    total_submissions = Submission.query.filter_by(task_id=task_id).count()
    if total_submissions >= (job.max_submissions or 20):
        return jsonify({"error": "Maximum submissions reached for this task"}), 400

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

    # Launch oracle in background thread
    t = threading.Thread(
        target=_run_oracle,
        args=(app, sub.id),
        daemon=True,
    )
    t.start()

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
def cancel_job(task_id):
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    data = request.get_json(silent=True) or {}
    buyer_id = data.get('buyer_id')
    if not buyer_id:
        return jsonify({"error": "buyer_id is required"}), 400
    if buyer_id != job.buyer_id:
        return jsonify({"error": "Only the job creator can cancel"}), 403

    if job.status != 'open':
        return jsonify({
            "error": f"Can only cancel open jobs (current: {job.status})"
        }), 400

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
def refund_job(task_id):
    from services.job_service import JobService
    from services.wallet_service import get_wallet_service

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    data = request.get_json(silent=True) or {}
    buyer_id = data.get('buyer_id')
    if not buyer_id:
        return jsonify({"error": "buyer_id is required"}), 400
    if buyer_id != job.buyer_id:
        return jsonify({"error": "Only the job creator can request a refund"}), 403

    if job.status not in ('expired', 'cancelled'):
        return jsonify({"error": f"Not refundable in state: {job.status}"}), 400

    # Attempt on-chain refund if wallet is connected and deposit info exists
    wallet = get_wallet_service()
    refund_tx = None
    if wallet.is_connected() and job.depositor_address and job.deposit_tx_hash:
        try:
            refund_tx = wallet.refund(job.depositor_address, job.price)
            job.refund_tx_hash = refund_tx
        except Exception as e:
            print(f"[Relay] Refund failed: {e}")
            return jsonify({"error": f"On-chain refund failed: {str(e)}"}), 500

    job.status = 'cancelled'
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
    app.run(port=5005, debug=True)
