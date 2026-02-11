from flask import Flask, request, jsonify, render_template_string, render_template
from models import db, Owner, Agent, Job, LedgerEntry
from config import Config
import os
import uuid
import json
import datetime
import hmac
import hashlib
from decimal import Decimal
from wallet_manager import wallet_manager
from sqlalchemy import text, inspect

from core.escrow_manager import EscrowManager

app = Flask(__name__)
app.config.from_object(Config)
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET')
if not WEBHOOK_SECRET:
    WEBHOOK_SECRET = 'dev-secret-DO-NOT-USE-IN-PRODUCTION'
    print("[Relay] WARNING: WEBHOOK_SECRET not set. Using insecure default. Set WEBHOOK_SECRET env var for production.")

db.init_app(app)


# Initialize Database
print("[Relay] Starting SYNAI Relay Protocol Service...")
with app.app_context():
    try:
        print(f"[Relay] Testing Database Connection...")
        db.create_all()
        # Migration Helper: Ensure wallet columns exist
        print("[Relay] Running lightweight migrations...")
        with db.engine.connect() as conn:
            inspector = inspect(db.engine)
            existing_columns = [col['name'] for col in inspector.get_columns('agents')]
            
            if 'wallet_address' not in existing_columns:
                print("[Relay] Adding wallet_address column to agents table...")
                conn.execute(text("ALTER TABLE agents ADD COLUMN wallet_address VARCHAR(42)"))
            
            if 'encrypted_privkey' not in existing_columns:
                print("[Relay] Adding encrypted_privkey column to agents table...")
                conn.execute(text("ALTER TABLE agents ADD COLUMN encrypted_privkey TEXT"))
            

            
            # Check Job table columns
            existing_job_columns = [col['name'] for col in inspector.get_columns('jobs')]
            if 'artifact_type' not in existing_job_columns:
                print("[Relay] Adding artifact_type to jobs table...")
                conn.execute(text("ALTER TABLE jobs ADD COLUMN artifact_type VARCHAR(20) DEFAULT 'CODE'"))

            if 'verification_config' not in existing_job_columns:
                print("[Relay] Adding verification_config to jobs table...")
                # SQLite doesn't support JSON type natively in ALTER TABLE easily, use TEXT
                conn.execute(text("ALTER TABLE jobs ADD COLUMN verification_config JSON"))

            if 'verifiers_config' not in existing_job_columns:
                print("[Relay] Adding verifiers_config to jobs table...")
                conn.execute(text("ALTER TABLE jobs ADD COLUMN verifiers_config JSON"))

            if 'deposit_amount' not in existing_job_columns:
                print("[Relay] Adding deposit_amount to jobs table...")
                conn.execute(text("ALTER TABLE jobs ADD COLUMN deposit_amount DECIMAL(20,6) DEFAULT 0"))

            if 'failure_count' not in existing_job_columns:
                print("[Relay] Adding failure_count to jobs table...")
                conn.execute(text("ALTER TABLE jobs ADD COLUMN failure_count INTEGER DEFAULT 0"))

            if 'solution_price' not in existing_job_columns:
                print("[Relay] Adding solution_price to jobs table...")
                conn.execute(text("ALTER TABLE jobs ADD COLUMN solution_price DECIMAL(20,6) DEFAULT 0"))

            if 'access_list' not in existing_job_columns:
                print("[Relay] Adding access_list to jobs table...")
                # SQLite workaround for JSON
                try:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN access_list JSON DEFAULT '[]'"))
                except:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN access_list TEXT DEFAULT '[]'"))

            # Check Agent table columns for metrics
            if 'metrics' not in existing_columns:
                print("[Relay] Adding metrics to agents table...")
                conn.execute(text("ALTER TABLE agents ADD COLUMN metrics JSON"))
                
            if 'locked_balance' not in existing_columns:
                print("[Relay] Adding locked_balance to agents table...")
                conn.execute(text("ALTER TABLE agents ADD COLUMN locked_balance DECIMAL(20,6) DEFAULT 0"))

            # Phase 1 migrations
            if 'expiry' not in existing_job_columns:
                print("[Relay] Adding expiry to jobs table...")
                conn.execute(text("ALTER TABLE jobs ADD COLUMN expiry DATETIME"))
            if 'max_retries' not in existing_job_columns:
                print("[Relay] Adding max_retries to jobs table...")
                conn.execute(text("ALTER TABLE jobs ADD COLUMN max_retries INTEGER DEFAULT 3"))
            if 'chain_task_id' not in existing_job_columns:
                print("[Relay] Adding chain_task_id to jobs table...")
                conn.execute(text("ALTER TABLE jobs ADD COLUMN chain_task_id VARCHAR(66)"))
            if 'verdict_data' not in existing_job_columns:
                print("[Relay] Adding verdict_data to jobs table...")
                conn.execute(text("ALTER TABLE jobs ADD COLUMN verdict_data JSON"))

            conn.commit()
            
        print("[Relay] Database check and migrations passed.")
    except Exception as e:
        print(f"[FATAL ERROR] Database initialization failed: {e}")

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy", "service": "synai-relay"}), 200

@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/dashboard')
def dashboard():
    return render_template('index.html')

@app.route('/docs')
def docs_html():
    """Human-readable documentation page."""
    # Simple markdown renderer could be added, or just serve raw text for now
    # Ideally should be a nice HTML page
    with open('templates/agent_manual.md', 'r') as f:
        content = f.read()
    html = f"""
    <html>
    <head><title>Synai Agent Manual</title>
    <style>body {{ font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.6; }} pre {{ background: #eee; padding: 10px; }} </style>
    </head>
    <body>
    <h1>Synai Relay Documentation</h1>
    <pre>{content}</pre>
    </body>
    </html>
    """
    return html

@app.route('/docs/agent')
def docs_agent():
    """Machine-readable documentation (System Prompt)."""
    with open('templates/agent_manual.md', 'r') as f:
        content = f.read()
    return content, 200, {'Content-Type': 'text/plain'}

@app.route('/install.md')
def install_script():
    return render_template('install.md')

@app.route('/auth/twitter')
def auth_twitter():
    # In a full app, this would redirect to Twitter OAuth
    # For now, we provide a smooth demo entry
    html = """
    <body style="background:#020202; color:#fff; font-family:sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; text-align:center; background-image: radial-gradient(circle at 50% 50%, rgba(188, 19, 254, 0.1) 0%, transparent 80%);">
        <div style="max-width:400px; padding:40px; border:1px solid rgba(255,255,255,0.1); border-radius:24px; background:rgba(255,255,255,0.03); backdrop-filter:blur(20px);">
            <div style="font-size:40px; margin-bottom:20px;">🐦</div>
            <h1 style="color:#bc13fe; margin-bottom:10px; font-size:24px;">DEMO AUTH MODE</h1>
            <p style="color:#888; line-height:1.6; font-size:14px; margin-bottom:30px;">Twitter API keys are not yet configured in production. You are entering as <b>Test_User_01</b>.</p>
            <a href="/dashboard" style="display:block; background:#bc13fe; color:#fff; text-decoration:none; padding:12px; border-radius:12px; font-weight:bold; transition:0.2s;">Enter Dashboard</a>
            <p style="margin-top:20px; font-size:10px; color:#555;">PROCESSED BY SYNAI SECURITY LAYER</p>
        </div>
    </body>
    """
    return render_template_string(html)

@app.route('/ledger/ranking', methods=['GET'])

def get_ranking():
    # Sort agents by balance (descending)
    agents = Agent.query.order_by(Agent.balance.desc()).limit(10).all()
    
    agent_ranking = []
    for a in agents:
        agent_ranking.append({
            "agent_id": a.agent_id,
            "balance": float(a.balance),
            "owner_id": a.owner.username if a.owner and not a.is_ghost else "[ENCRYPTED]",
            "owner_twitter": a.owner.twitter_handle if a.owner else None,
            "wallet_address": a.wallet_address,
            "is_ghost": a.is_ghost,
            "metrics": a.metrics or {"engineering": 0, "reliability": 0}
        })
    
    # Platform Stats
    total_agents = Agent.query.count()
    total_bounty_volume = db.session.query(db.func.sum(Job.price)).scalar() or 0
    active_tasks = Job.query.filter(Job.status.notin_(['settled', 'cancelled', 'refunded', 'expired'])).count()
    platform_revenue = db.session.query(db.func.sum(LedgerEntry.amount)).filter(LedgerEntry.target_id == 'platform_admin').scalar() or 0
    
    # Aggregate by owner for owner ranking
    unique_owners = Owner.query.all()
    owner_ranking = []
    for o in unique_owners:
        total_profit = sum(float(a.balance) for a in o.agents)
        owner_ranking.append({
            "owner_id": o.username,
            "total_profit": total_profit
        })
    owner_ranking.sort(key=lambda x: x['total_profit'], reverse=True)
    
    return jsonify({
        "stats": {
            "total_agents": total_agents,
            "total_bounty_volume": float(total_bounty_volume),
            "active_tasks": active_tasks
        },
        "agent_ranking": agent_ranking,
        "owner_ranking": owner_ranking[:10],
        "platform_revenue": float(platform_revenue)
    }), 200

@app.route('/ledger/<agent_id>', methods=['GET'])
def get_balance(agent_id):
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        return jsonify({"balance": 0.0}), 200
    return jsonify({"balance": float(agent.balance)}), 200

@app.route('/jobs', methods=['POST'])
def post_job():
    data = request.json
    try:
        # Parse expiry (unix timestamp) if provided
        expiry = None
        if data.get('expiry'):
            expiry = datetime.datetime.utcfromtimestamp(int(data['expiry']))

        new_job = Job(
            title=data.get('title', 'Untitled Task'),
            description=data.get('description', ''),
            price=Decimal(str(data.get('terms', {}).get('price', 0))),
            buyer_id=data.get('buyer_id', 'unknown'),
            artifact_type=data.get('artifact_type', 'CODE'),
            verification_config=data.get('verification_config', {}),
            verifiers_config=data.get('verifiers_config', []),
            envelope_json=data.get('envelope_json', {}),
            expiry=expiry,
            max_retries=data.get('max_retries', 3),
            status='created'
        )

        # Calculate required stake (5% of price)
        if not new_job.deposit_amount:
            new_job.deposit_amount = new_job.price * Decimal('0.05')

        db.session.add(new_job)
        db.session.commit()
        return jsonify({"status": "created", "task_id": str(new_job.task_id)}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400

@app.route('/jobs/<task_id>/fund', methods=['POST'])
def fund_job(task_id):
    from services.job_service import JobService
    from services.chain_bridge import get_chain_bridge

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.status == 'expired':
        return jsonify({"error": "Task expired", "status": "expired"}), 410
    if job.status != 'created':
        return jsonify({"error": f"Job not in created state (current: {job.status})"}), 400

    tx_hash = request.json.get('escrow_tx_hash')

    # On-chain funding via ChainBridge (if connected and task has chain_task_id)
    if not tx_hash:
        bridge = get_chain_bridge()
        if bridge.is_connected() and job.chain_task_id:
            boss_key = request.json.get('boss_key')
            if not boss_key:
                return jsonify({"error": "boss_key or escrow_tx_hash required"}), 400
            try:
                tx_hash = bridge.fund_task(boss_key, job.chain_task_id)
                print(f"[ChainBridge] Task {task_id} funded on-chain, tx: {tx_hash}")
            except Exception as e:
                print(f"[ChainBridge] On-chain fund_task failed: {e}")
                return jsonify({"error": f"On-chain funding failed: {str(e)}"}), 500

    if not tx_hash:
        return jsonify({"error": "Escrow transaction hash required"}), 400

    job.status = 'funded'
    job.escrow_tx_hash = tx_hash
    db.session.commit()
    return jsonify({"status": "funded", "tx_hash": tx_hash}), 200

@app.route('/jobs/<task_id>/claim', methods=['POST'])
def claim_job(task_id):
    from services.job_service import JobService
    from services.chain_bridge import get_chain_bridge

    agent_id = request.json.get('agent_id')
    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Lazy expiry check (already done by JobService.get_job, but check result)
    if job.status == 'expired':
        return jsonify({"error": "Task expired", "status": "expired"}), 410

    # Strictly enforce funding check
    if job.status != 'funded':
        # Allow retry if status is 'rejected' and retries remain
        if job.status == 'rejected' and job.failure_count < (job.max_retries or 3):
            pass  # Allow retry
        else:
            return jsonify({"error": f"Job not available (Status: {job.status})"}), 403

    # Circuit Breaker Check
    max_retries = job.max_retries or 3
    if job.failure_count >= max_retries:
        job.status = 'expired'
        db.session.commit()
        return jsonify({"error": "Job expired due to too many failures."}), 403

    # Require agent to be registered (no auto-register)
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        return jsonify({"error": "Agent not registered. Call POST /agents/register first."}), 400

    # Financial: Require Stake
    required_stake = job.deposit_amount
    try:
        EscrowManager.stake_funds(agent.agent_id, required_stake, job.task_id)
        print(f"[Relay] Agent {agent_id} staked {required_stake} for task {task_id}")
    except ValueError as e:
        return jsonify({"error": f"Staking failed: {str(e)}"}), 400

    # On-chain claim via ChainBridge
    chain_tx_hash = None
    bridge = get_chain_bridge()
    if bridge.is_connected() and job.chain_task_id:
        try:
            if agent.encrypted_privkey:
                worker_key = wallet_manager.decrypt_privkey(agent.encrypted_privkey)
                chain_tx_hash = bridge.claim_task(worker_key, job.chain_task_id)
                print(f"[ChainBridge] Task {task_id} claimed on-chain by {agent_id}, tx: {chain_tx_hash}")
            else:
                print(f"[ChainBridge] Agent {agent_id} has no encrypted_privkey, skipping on-chain claim")
        except Exception as e:
            print(f"[ChainBridge] On-chain claim_task failed: {e}")

    job.status = 'claimed'
    job.claimed_by = agent_id
    db.session.commit()
    response = {"status": "claimed", "message": f"Job claimed & staked by {agent_id}"}
    if chain_tx_hash:
        response["chain_tx_hash"] = chain_tx_hash
    return jsonify(response), 200


@app.route('/jobs/<task_id>/submit', methods=['POST'])
def submit_result(task_id):
    """Worker submits task result for verification."""
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Task not found"}), 404
    if job.status not in ('claimed', 'rejected'):
        return jsonify({"error": f"Task not submittable, current: {job.status}"}), 400

    data = request.json or {}
    agent_id = data.get('agent_id')
    result = data.get('result', {})
    if not agent_id:
        return jsonify({"error": "agent_id required"}), 400
    if agent_id != job.claimed_by:
        return jsonify({"error": "Only the assigned agent can submit"}), 403

    job.status = 'submitted'
    job.result_data = result
    db.session.flush()
    print(f"[Relay] Task {task_id} submitted by {agent_id}.")

    # On-chain submit via ChainBridge
    from services.chain_bridge import get_chain_bridge
    bridge = get_chain_bridge()
    if bridge.is_connected() and job.chain_task_id:
        try:
            agent = Agent.query.filter_by(agent_id=agent_id).first()
            if agent and agent.encrypted_privkey:
                worker_key = wallet_manager.decrypt_privkey(agent.encrypted_privkey)
                result_bytes = json.dumps(result, sort_keys=True).encode('utf-8')
                result_hash = bytes.fromhex(hashlib.sha256(result_bytes).hexdigest())
                chain_tx_hash = bridge.submit_result(worker_key, job.chain_task_id, result_hash)
                print(f"[ChainBridge] Task {task_id} result submitted on-chain, tx: {chain_tx_hash}")
            else:
                print(f"[ChainBridge] Agent {agent_id} has no encrypted_privkey, skipping on-chain submit")
        except Exception as e:
            print(f"[ChainBridge] On-chain submit_result failed: {e}")

    # Branch: auto-verify or manual
    if job.verifiers_config:
        try:
            from services.verification import VerificationService
            combined = VerificationService.verify_and_settle(job, result)
            return jsonify(combined), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": f"Verification internal error: {str(e)}"}), 500
    else:
        # No verifiers: wait for manual /confirm
        db.session.commit()
        return jsonify({
            "status": "submitted",
            "message": "Awaiting manual confirmation via POST /jobs/:id/confirm"
        }), 200

def _settle_job(job, success=True):
    """Thin wrapper delegating to SettlementService."""
    from services.settlement import SettlementService
    if success:
        return SettlementService.settle_success(job)
    else:
        return SettlementService.settle_reject(job)


@app.route('/v1/verify/webhook/<task_id>', methods=['POST'])
def webhook_callback(task_id):
    signature = request.headers.get('X-Signature', '')
    body = request.get_data()
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return jsonify({"error": "Invalid signature"}), 403

    job = Job.query.filter_by(task_id=task_id).first()
    if not job:
        return jsonify({"error": "Task not found"}), 404
        
    if job.status not in ['claimed', 'submitted', 'funded']:
        return jsonify({"error": "Task not in executable state"}), 400

    # Payload from external source
    payload = request.json or {}
    
    # Update Job Result with Webhook Data (Persist for Composite Verifier)
    current_result = dict(job.result_data or {})
    current_result['webhook_payload'] = payload
    job.result_data = current_result
    db.session.commit()
    
    # Dispatch to VerificationService (same pipeline as submit endpoint)
    try:
        from services.verification import VerificationService
        combined = VerificationService.verify_and_settle(job, current_result)
        return jsonify(combined), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/jobs/<task_id>/confirm', methods=['POST'])
def confirm_job(task_id):
    """Manual confirmation endpoint (used when verifiers_config is empty)."""
    from services.job_service import JobService
    from services.settlement import SettlementService

    buyer_id = request.json.get('buyer_id')
    signature = request.json.get('signature')
    job = JobService.get_job(task_id)

    if not job or job.buyer_id != buyer_id:
        return jsonify({"error": "Unauthorized"}), 403

    if job.status != 'submitted':
        return jsonify({"error": "Job not in submitted state"}), 400

    if not signature:
        return jsonify({"error": "Acceptance signature required for release"}), 400

    job.signature = signature
    job.status = 'accepted'
    result = SettlementService.settle_success(job)

    # On-chain settle via ChainBridge (manual confirmation path)
    from services.chain_bridge import get_chain_bridge
    bridge = get_chain_bridge()
    if bridge.is_connected() and job.chain_task_id:
        try:
            chain_tx_hash = bridge.settle(job.chain_task_id, bridge.oracle_private_key)
            result["chain_settle_tx"] = chain_tx_hash
            print(f"[ChainBridge] Task {task_id} settled on-chain, tx: {chain_tx_hash}")
        except Exception as e:
            print(f"[ChainBridge] On-chain settle failed: {e}")

    print(f"[Relay] Boss {buyer_id} confirmed task {task_id}. Settlement complete.")
    return jsonify({"status": "settled", **result}), 200

@app.route('/agents/adopt', methods=['POST'])
def adopt_agent():
    data = request.json
    agent_id = data.get('agent_id')
    twitter_handle = data.get('twitter_handle')
    tweet_url = data.get('tweet_url')

    if not agent_id or not twitter_handle:
        return jsonify({"error": "agent_id and twitter_handle are required"}), 400

    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    # Find or create Owner
    owner = Owner.query.filter_by(twitter_handle=twitter_handle).first()
    if not owner:
        owner_id = f"owner_{uuid.uuid4().hex[:8]}"
        owner = Owner(
            owner_id=owner_id,
            username=twitter_handle,
            twitter_handle=twitter_handle
        )
        db.session.add(owner)
    
    agent.owner_id = owner.owner_id
    agent.adoption_tweet_url = tweet_url
    agent.adopted_at = datetime.datetime.utcnow()
    
    db.session.commit()
    print(f"[Relay] Agent {agent_id} adopted by @{twitter_handle}")
    return jsonify({"status": "success", "message": f"Agent {agent_id} adopted by @{twitter_handle}"}), 200

@app.route('/agents/register', methods=['POST'])
def register_agent():
    """Explicit agent registration with wallet creation."""
    from services.agent_service import AgentService
    data = request.json or {}
    agent_id = data.get('agent_id')
    name = data.get('name')
    if not agent_id:
        return jsonify({"error": "agent_id required"}), 400
    try:
        profile = AgentService.register(agent_id, name)
        return jsonify({"status": "registered", **profile}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

@app.route('/agents/<agent_id>', methods=['GET'])
def get_agent_profile(agent_id):
    """Agent profile: balance, locked_balance, metrics, wallet."""
    from services.agent_service import AgentService
    profile = AgentService.get_profile(agent_id)
    if not profile:
        return jsonify({"error": "Agent not found"}), 404
    return jsonify(profile), 200

@app.route('/agents/<agent_id>/deposit', methods=['POST'])
def deposit_funds(agent_id):
    """Deposit funds into agent balance. Auto-registers if not found."""
    from services.agent_service import AgentService

    data = request.json or {}
    raw_amount = data.get('amount')
    try:
        amount = Decimal(str(raw_amount))
        if not amount.is_finite() or amount <= 0:
            return jsonify({"error": "Positive finite amount required"}), 400
    except Exception:
        return jsonify({"error": "Invalid amount"}), 400

    try:
        result = AgentService.deposit(agent_id, amount)
        return jsonify({"status": "deposited", **result}), 200
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Transaction failed"}), 500

@app.route('/jobs', methods=['GET'])
def list_jobs():
    from services.job_service import JobService
    status = request.args.get('status')
    buyer_id = request.args.get('buyer_id')
    claimed_by = request.args.get('claimed_by')
    jobs = JobService.list_jobs(status=status, buyer_id=buyer_id, claimed_by=claimed_by)
    return jsonify([JobService.to_dict(j) for j in jobs]), 200

@app.route('/jobs/<task_id>', methods=['GET'])
def get_job(task_id):
    from services.job_service import JobService
    job = JobService.get_job(task_id)  # includes lazy expiry check
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Knowledge Monetization: Access Control
    can_access = True
    buyer_id = request.args.get('buyer_id') or request.headers.get('X-Agent-ID')

    if job.status == 'settled' and job.solution_price and job.solution_price > 0:
        access_list = job.access_list or []
        is_owner = (buyer_id == job.buyer_id) or (buyer_id == job.claimed_by)
        has_paid = buyer_id in access_list
        if not (is_owner or has_paid):
            can_access = False

    result = job.result_data
    if not can_access:
        result = {"preview": "LOCKED CONTENT", "buy_to_unlock": float(job.solution_price)}

    base = JobService.to_dict(job)
    base["result"] = result
    base["solution_price"] = float(job.solution_price) if job.solution_price else 0
    base["is_locked"] = not can_access
    return jsonify(base), 200

@app.route('/jobs/<task_id>/cancel', methods=['POST'])
def cancel_job(task_id):
    """Boss cancels a pre-claim task."""
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Task not found"}), 404

    buyer_id = request.json.get('buyer_id')
    if not buyer_id or buyer_id != job.buyer_id:
        return jsonify({"error": "Only the task creator can cancel"}), 403

    if job.status not in ('created', 'funded'):
        return jsonify({"error": f"Cannot cancel task in state: {job.status}"}), 400
    if job.claimed_by:
        return jsonify({"error": "Cannot cancel: task already claimed by a worker"}), 400

    # On-chain cancel via chain_bridge (optional, graceful degradation)
    from services.chain_bridge import get_chain_bridge
    bridge = get_chain_bridge()
    if bridge.is_connected() and job.chain_task_id:
        try:
            boss_key = (request.json or {}).get('boss_key')
            if boss_key:
                bridge.cancel_task(boss_key, job.chain_task_id)
        except Exception as e:
            return jsonify({"error": f"On-chain cancel failed: {str(e)}"}), 500

    job.status = 'cancelled'
    db.session.commit()
    return jsonify({"status": "cancelled", "task_id": task_id}), 200

@app.route('/jobs/<task_id>/refund', methods=['POST'])
def refund_job(task_id):
    """Boss reclaims funds from expired/cancelled task."""
    from services.job_service import JobService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Task not found"}), 404

    buyer_id = request.json.get('buyer_id')
    if not buyer_id or buyer_id != job.buyer_id:
        return jsonify({"error": "Only the task creator can request refund"}), 403

    if job.status not in ('expired', 'cancelled'):
        return jsonify({"error": f"Not refundable in state: {job.status}"}), 400

    # On-chain refund via chain_bridge (optional, graceful degradation)
    from services.chain_bridge import get_chain_bridge
    bridge = get_chain_bridge()
    if bridge.is_connected() and job.chain_task_id:
        try:
            boss_key = (request.json or {}).get('boss_key')
            if boss_key:
                bridge.refund(boss_key, job.chain_task_id)
        except Exception as e:
            return jsonify({"error": f"On-chain refund failed: {str(e)}"}), 500

    # Release any locked worker stake (if worker existed)
    if job.claimed_by and job.deposit_amount:
        try:
            EscrowManager.release_stake(job.claimed_by, job.deposit_amount, job.task_id)
        except Exception:
            pass  # Stake may already have been released

    job.status = 'refunded'
    db.session.commit()
    return jsonify({
        "status": "refunded",
        "task_id": task_id,
        "amount": float(job.price),
    }), 200

@app.route('/jobs/<task_id>/verdict', methods=['GET'])
def get_verdict(task_id):
    """Query CVS verdict details for a task."""
    from services.job_service import JobService
    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Task not found"}), 404
    if not job.verdict_data:
        return jsonify({"error": "No verdict available"}), 404
    return jsonify(job.verdict_data), 200

@app.route('/agents/<agent_id>/withdraw', methods=['POST'])
def withdraw_funds(agent_id):
    """Agent withdraws on-chain funds via TaskEscrow.withdraw()."""
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    # On-chain withdrawal via chain_bridge (optional, graceful degradation)
    from services.chain_bridge import get_chain_bridge
    bridge = get_chain_bridge()
    chain_tx = None
    if bridge.is_connected() and agent.wallet_address:
        try:
            pending = bridge.get_pending_withdrawal(agent.wallet_address)
            if pending > 0:
                from wallet_manager import wallet_manager
                priv_key = wallet_manager.decrypt_privkey(agent.encrypted_privkey)
                chain_tx = bridge.withdraw(priv_key)
        except Exception as e:
            print(f"[ChainBridge] On-chain withdraw failed: {e}")

    if not bridge.is_connected():
        return jsonify({"error": "Chain bridge not connected. On-chain withdrawal not yet available."}), 503

    response = {
        "status": "withdrawn",
        "agent_id": agent_id,
        "wallet_address": agent.wallet_address,
    }
    if chain_tx:
        response["chain_tx_hash"] = chain_tx
    return jsonify(response), 200

@app.route('/jobs/<task_id>/unlock', methods=['POST'])
def unlock_solution(task_id):
    buyer_id = request.json.get('agent_id')
    if not buyer_id:
        return jsonify({"error": "Agent ID required"}), 400
        
    job = Job.query.filter_by(task_id=task_id).first()
    if not job:
        return jsonify({"error": "Job not found"}), 404
        
    if job.status != 'settled':
        return jsonify({"error": "Solution not ready (Job not settled)"}), 400
        
    if job.solution_price <= 0:
        return jsonify({"status": "success", "message": "Solution is free"}), 200
        
    access_list = list(job.access_list or [])
    if buyer_id in access_list:
        return jsonify({"status": "success", "message": "Already unlocked"}), 200
        
    # Financial Transaction
    buyer = Agent.query.filter_by(agent_id=buyer_id).first()
    if not buyer or buyer.balance < job.solution_price:
        return jsonify({"error": "Insufficient funds"}), 402
        
    # Transfer: Buyer -> Solver (80%) + Platform (20%)
    price = job.solution_price
    platform_fee = price * Decimal('0.20')
    solver_payout = price * Decimal('0.80')
    
    solver = Agent.query.filter_by(agent_id=job.claimed_by).first()
    
    buyer.balance -= price
    if solver:
        solver.balance += solver_payout
        
    # Update Ledger
    db.session.add(LedgerEntry(source_id=buyer_id, target_id=job.claimed_by, amount=solver_payout, transaction_type='solution_purchase', task_id=task_id))
    db.session.add(LedgerEntry(source_id=buyer_id, target_id='platform_admin', amount=platform_fee, transaction_type='platform_fee', task_id=task_id))
    
    # Update Access List
    access_list.append(buyer_id)
    job.access_list = access_list # specific for SQLAlchemy JSON mutable tracking?
    # Simple fix: reassign
    # Actually, SQLAlchemy tracks mutation on JSON if using mutable extension, but reassign is safer
    
    db.session.commit()
    
    return jsonify({"status": "success", "message": "Solution unlocked"}), 200

# Agent Adoption Verification (Tweet-to-Adopt)
@app.route('/share/job/<task_id>', methods=['GET'])
def share_job(task_id):
    job = Job.query.filter_by(task_id=task_id).first()
    if not job:
        return "Task not found", 404
        
    # Extract technical details from envelope
    env = job.envelope_json or {}
    payload = env.get('payload', {})
    criteria = payload.get('verification_regex', 'N/A')
    entrypoint = payload.get('entrypoint', 'N/A')
    env_setup = payload.get('environment_setup', 'Standard ATP Node v1')
    
    # A high-fidelity technical sharing page
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>SYNAI.SHOP - {job.title}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
            body {{ background: #050505; color: #e1e1e1; font-family: 'Inter', sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px; }}
            .card {{ background: rgba(15,15,20,0.9); border: 1px solid rgba(0,243,255,0.3); padding: 40px; border-radius: 24px; text-align: left; max-width: 600px; width: 100%; box-shadow: 0 0 50px rgba(0,243,255,0.1); backdrop-filter: blur(10px); }}
            .brand {{ color: #00ff41; font-family: 'JetBrains Mono', monospace; font-size: 12px; letter-spacing: 2px; margin-bottom: 20px; }}
            h1 {{ color: #fff; font-size: 28px; margin: 0 0 10px 0; letter-spacing: -1px; }}
            .desc {{ color: #888; font-size: 15px; line-height: 1.6; margin-bottom: 30px; }}
            .price-row {{ display: flex; justify-content: space-between; align-items: center; padding: 20px; background: rgba(188,19,254,0.05); border-left: 4px solid #bc13fe; border-radius: 8px; margin-bottom: 30px; }}
            .price-val {{ font-size: 32px; font-family: 'JetBrains Mono', monospace; color: #bc13fe; font-weight: bold; }}
            .tech-specs {{ background: rgba(255,255,255,0.03); padding: 20px; border-radius: 12px; font-family: 'JetBrains Mono', monospace; font-size: 13px; border: 1px solid rgba(255,255,255,0.05); }}
            .spec-item {{ margin-bottom: 15px; }}
            .spec-label {{ color: #555; text-transform: uppercase; font-size: 10px; margin-bottom: 5px; }}
            .spec-val {{ color: #00f3ff; word-break: break-all; }}
            .btn {{ display: block; text-align: center; padding: 15px; background: #00f3ff; color: #000; text-decoration: none; border-radius: 8px; margin-top: 30px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; transition: 0.2s; }}
            .btn:hover {{ background: #fff; box-shadow: 0 0 20px #00f3ff; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="brand">● SYNAI.SHOP // TASK_MANIFEST_v1.0</div>
            <h1>{job.title}</h1>
            <p class="desc">{job.description or 'Autonomous task requiring specialized execution and verification.'}</p>
            
            <div class="price-row">
                <span style="font-size: 11px; color: #bc13fe; font-weight: 800;">BOUNTY</span>
                <span class="price-val">{float(job.price)} USDC</span>
            </div>

            <div class="tech-specs">
                <div class="spec-item">
                    <div class="spec-label">Acceptance Criteria (Regex)</div>
                    <div class="spec-val">{criteria}</div>
                </div>
                <div class="spec-item">
                    <div class="spec-label">Entrypoint / Verifier</div>
                    <div class="spec-val">{entrypoint}</div>
                </div>
                <div class="spec-item">
                    <div class="spec-label">Target Environment</div>
                    <div class="spec-val">{env_setup}</div>
                </div>
            </div>

            <a href="https://synai.shop" class="btn">Deploy Solution</a>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)



if __name__ == "__main__":
    app.run(port=5005, debug=True)
