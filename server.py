from flask import Flask, request, jsonify, render_template_string, render_template
from models import db, Owner, Agent, Job, LedgerEntry
from config import Config
import os
import uuid
import datetime
from decimal import Decimal
from wallet_manager import wallet_manager
from sqlalchemy import text, inspect

from core.verifier_factory import VerifierFactory
from core.escrow_manager import EscrowManager

app = Flask(__name__)
app.config.from_object(Config)

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

            # Check Agent table columns for metrics
            if 'metrics' not in existing_columns:
                print("[Relay] Adding metrics to agents table...")
                conn.execute(text("ALTER TABLE agents ADD COLUMN metrics JSON"))
                
            if 'locked_balance' not in existing_columns:
                print("[Relay] Adding locked_balance to agents table...")
                conn.execute(text("ALTER TABLE agents ADD COLUMN locked_balance DECIMAL(20,6) DEFAULT 0"))
            
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
    active_tasks = Job.query.filter(Job.status != 'completed').count()
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
        new_job = Job(
            title=data.get('title', 'Untitled Task'),
            description=data.get('description', ''),
            price=Decimal(str(data.get('terms', {}).get('price', 0))),
            buyer_id=data.get('buyer_id', 'unknown'),

            
            # New Fields
            artifact_type=data.get('artifact_type', 'CODE'),
            verification_config=data.get('verification_config', {}),
            verifiers_config=data.get('verifiers_config', []),
            
            envelope_json=data.get('envelope_json', {}),
            status='posted'
        )
        
        # Calculate required stake (e.g. 10% of price)
        if not new_job.deposit_amount:
            new_job.deposit_amount = new_job.price * Decimal('0.10')
            
        db.session.add(new_job)
        db.session.commit()
        return jsonify({"status": "posted", "task_id": str(new_job.task_id)}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400

@app.route('/jobs/<task_id>/fund', methods=['POST'])
def fund_job(task_id):
    tx_hash = request.json.get('escrow_tx_hash')
    job = Job.query.filter_by(task_id=task_id).first()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    if not tx_hash:
        return jsonify({"error": "Escrow transaction hash required"}), 400
        
    job.status = 'funded'
    job.escrow_tx_hash = tx_hash
    db.session.commit()
    return jsonify({"status": "funded", "tx_hash": tx_hash}), 200

@app.route('/jobs/<task_id>/claim', methods=['POST'])
def claim_job(task_id):
    agent_id = request.json.get('agent_id')
    job = Job.query.filter_by(task_id=task_id).first()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    # Strictly enforce funding check
    if job.status != 'funded':
        # Allow retry if status is 'failed' but not 'paused' or 'slashed'
        # Actually, if status is 'failed', it resets to 'funded' or stays 'failed'?
        # Let's say we allow claiming 'failed' tasks if failure_count < 3
        if job.status == 'failed' and job.failure_count < 3:
            pass # Allow retry
        else:
            return jsonify({"error": f"Job not available (Status: {job.status})"}), 403

    # Circuit Breaker Check
    if job.failure_count >= 3:
        job.status = 'paused'
        db.session.commit()
        return jsonify({"error": "Job paused due to too many failures. Contact Requester."}), 403
    
    # Auto-register agent if not exists
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        print(f"[Relay] New agent detected: {agent_id}. Registering with managed wallet...")
        addr, enc_key = wallet_manager.create_wallet()
        agent = Agent(
            agent_id=agent_id, 
            name=f"Agent_{agent_id[:6]}", 
            balance=0,
            wallet_address=addr,
            encrypted_privkey=enc_key
        )
        db.session.add(agent)
    
    # Financial: Require Stake
    required_stake = job.deposit_amount
    try:
        EscrowManager.stake_funds(agent.agent_id, required_stake, job.task_id)
        print(f"[Relay] Agent {agent_id} staked {required_stake} for task {task_id}")
    except ValueError as e:
        return jsonify({"error": f"Staking failed: {str(e)}"}), 400

    job.status = 'claimed' # In 2.0 this conceptually maps to 'STAKED'
    job.claimed_by = agent_id
    db.session.commit()
    return jsonify({"status": "success", "message": f"Job claimed & staked by {agent_id}"}), 200


    # Updated to trigger Auto-Verification
    job.status = 'submitted'
    job.result_data = result
    db.session.commit()
    
    print(f"[Relay] Task {task_id} submitted. Dispatching to Verifier...")
    
    # Dispatch Verification
    try:
        # Use Composite Verification
        verification_result = VerifierFactory.verify_composite(job, result)
        
        score = verification_result['score']
        is_passing = verification_result['success']
        print(f"[Relay] Verification Result for {task_id}: Score={score}, Pass={is_passing}")

        if is_passing:
            print(f"[Relay] Verification PASSED. Triggering Settlement...")
            payout_info = _settle_job(job, success=True)
            return jsonify({
                "status": "completed", 
                "verification": verification_result,
                "settlement": payout_info
            }), 200
        else:
            print(f"[Relay] Verification FAILED (Score {score}). executing Failure Logic...")
            # For now, treat low score as 'Ordinary Failure' -> Refund Stake minus Fee
            payout_info = _settle_job(job, success=False)
            return jsonify({
                "status": "failed", 
                "message": "Verification Failed - Stake Penalized",
                "verification": verification_result,
                "settlement": payout_info
            }), 200

    except Exception as e:
        print(f"[Relay] Verification System Error: {e}")
        return jsonify({"error": f"Verification internal error: {str(e)}"}), 500

def _settle_job(job, success=True):
    """
    Internal helper to execute atomic settlement with Staking logic.
    """
    if job.status in ['completed', 'slashed', 'failed']:
        return {"error": "Already settled"}
        
    agent_id = job.claimed_by
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    
    if success:
        # 1. Release Reward
        price = job.price
        platform_fee = price * Decimal('0.20')
        seller_payout = price * Decimal('0.80')
        
        if agent:
            agent.balance += seller_payout
            
            # Ledger: Payout
            db.session.add(LedgerEntry(
                source_id='platform', target_id=agent_id,
                amount=seller_payout, transaction_type='task_payout', task_id=job.task_id
            ))
            # Ledger: Fee
            db.session.add(LedgerEntry(
                source_id='platform', target_id='platform_admin',
                amount=platform_fee, transaction_type='platform_fee', task_id=job.task_id
            ))
            
            # 2. Release Stake
            EscrowManager.release_stake(agent_id, job.deposit_amount, job.task_id)

            # Reputation
            metrics = agent.metrics or {"engineering": 0, "creativity": 0, "reliability": 0}
            metrics['reliability'] = metrics.get('reliability', 0) + 1
            agent.metrics = metrics

        job.status = 'completed'
        
        return {"payout": float(seller_payout), "fee": float(platform_fee), "stake_return": float(job.deposit_amount)}

    else:
        # Failure Logic
        # Refund Stake minus 5% Penalty
        stake_amount = job.deposit_amount
        penalty = stake_amount * Decimal('0.05')
        refund = stake_amount - penalty
        
        if agent:
            # Release Refund
            EscrowManager.release_stake(agent_id, refund, job.task_id)
            # Slash Penalty (Technically this part of locked balance is just moved to treasury)
            # But release_stake only moves what we ask. The remaining 'penalty' matches locked_balance?
            # Creating a slash entry for the penalty to keep ledger clean? 
            # Actually EscrowManager.release_stake moves FROM locked. 
            # We need to explicitly Slash the penalty.
            EscrowManager.slash_stake(agent_id, penalty, job.task_id, reason="Verification Failure Penalty")
            
            # Reputation Hit
            metrics = agent.metrics or {"engineering": 0, "creativity": 0, "reliability": 0}
            metrics['reliability'] = max(0, metrics.get('reliability', 0) - 1)
            agent.metrics = metrics
            
        job.status = 'failed' # Or 'open' to retry? Let's say failed for now.
        job.failure_count += 1
        
        return {"payout": 0, "fee": 0, "stake_return": float(refund), "penalty": float(penalty)}


@app.route('/v1/verify/webhook/<task_id>', methods=['POST'])
def webhook_callback(task_id):
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
    
    # Dispatch to Verifier
    try:
        # Use Composite Verification
        verification_result = VerifierFactory.verify_composite(job, current_result)
        
        score = verification_result['score']
        is_passing = verification_result['success']
        
        if is_passing:
            _settle_job(job, success=True)
            return jsonify({"status": "verified", "message": "Callback accepted, task settled."}), 200
        else:
            # Logic: Webhook arrived, but maybe score is still low (e.g. other verifiers failed previously?)
            # Or maybe the payload didn't match?
            # We should probably treat this as a failure attempt if it was meant to be the final trigger.
            # Let's settle as fail to penalize if appropriate, or just return status.
            # User requirement: "Timeout if not received" -> Slash.
            # If received but wrong -> Simple fail?
            _settle_job(job, success=False)
            return jsonify({"status": "rejected", "reason": verification_result.get('reason')}), 400
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/jobs/<task_id>/confirm', methods=['POST'])
def confirm_job(task_id):
    # Legacy / Manual Confirm endpoint
    # ... (existing code, maybe deprecated?)
    pass
    buyer_id = request.json.get('buyer_id')
    signature = request.json.get('signature')
    job = Job.query.filter_by(task_id=task_id).first()
    
    if not job or job.buyer_id != buyer_id:
        return jsonify({"error": "Unauthorized"}), 403
    
    if job.status != 'submitted':
        return jsonify({"error": "Job not in submitted state"}), 400
        
    if not signature:
        return jsonify({"error": "Acceptance signature required for release"}), 400

    # Settlement with 20% Platform Fee
    price = job.price
    platform_fee = price * Decimal('0.20')
    seller_payout = price * Decimal('0.80')
    agent_id = job.claimed_by
    
    job.signature = signature
    
    print(f"[DEBUG] Settling Task {task_id}: Price={price}, Payout={seller_payout}, Fee={platform_fee}")
    
    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if agent:
        print(f"[DEBUG] Old Balance for {agent_id}: {agent.balance}")
        agent.balance += seller_payout
        print(f"[DEBUG] New Balance for {agent_id}: {agent.balance}")

        
        # Log Ledger Entries
        payout_entry = LedgerEntry(
            source_id='platform',
            target_id=agent_id,
            amount=seller_payout,
            transaction_type='task_payout',
            task_id=job.task_id
        )
        fee_entry = LedgerEntry(
            source_id='platform',
            target_id='platform_admin',
            amount=platform_fee,
            transaction_type='platform_fee',
            task_id=job.task_id
        )
        db.session.add(payout_entry)
        db.session.add(fee_entry)
    
    job.status = 'completed'
    db.session.commit()
    
    print(f"[Relay] Proxy {buyer_id} confirmed task {task_id}. Settlement complete.")
    return jsonify({
        "status": "success", 
        "payout": float(seller_payout),
        "fee": float(platform_fee)
    }), 200

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

@app.route('/jobs', methods=['GET'])
def list_jobs():
    all_jobs = Job.query.all()
    return jsonify([{
        "task_id": str(j.task_id),
        "title": j.title,
        "price": float(j.price),
        "status": j.status,
        "claimed_by": j.claimed_by,
        "artifact_type": j.artifact_type,
        "deposit_amount": float(j.deposit_amount) if j.deposit_amount else 0,
        "verifiers_config": j.verifiers_config,
        "result_data": j.result_data,
        "failure_count": j.failure_count
    } for j in all_jobs]), 200

@app.route('/jobs/<task_id>', methods=['GET'])
def get_job(task_id):
    job = Job.query.filter_by(task_id=task_id).first()
    if job:
        return jsonify({
            "task_id": str(job.task_id),
            "title": job.title,
            "description": job.description,
            "price": float(job.price),
            "status": job.status,
            "claimed_by": job.claimed_by,
            "result": job.result_data
        }), 200
    return jsonify({"error": "Job not found"}), 404

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
