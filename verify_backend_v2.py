from server import app, db, Agent, Job, EscrowManager, VerifierFactory
from decimal import Decimal
import time
import json
import uuid

def test_v2_logic():
    print("=== Testing Synai V2 Logic via Direct DB Access ===")
    
    with app.app_context():
        # 0. Setup
        agent_id = f"worker_{uuid.uuid4().hex[:6]}"
        buyer_id = f"buyer_{uuid.uuid4().hex[:6]}"
        
        # 1. Create Agent and Fund (Simulate Faucet)
        print(f"[1] Creating Agent {agent_id} with 100 USDC...")
        agent = Agent(agent_id=agent_id, name="TestWorker", balance=100)
        db.session.add(agent)
        db.session.commit()
        
        # 2. Create Job
        print("[2] Creating Job...")
        job = Job(
            title="V2 Test Task",
            price=Decimal("50.00"),
            buyer_id=buyer_id,
            # Test Composite: Webhook (100%) since Docker is missing
            verifiers_config=[
                {"type": "webhook", "weight": 1.0, "config": {"expected_payload": {"status": "ok"}}}
            ]
        )
        # Server auto-sets deposit to 10% (5.00) in post_job ONLY via API
        # Here manually set
        job.deposit_amount = Decimal("5.00")
        job.status = 'funded' # Skip funding step for speed
        db.session.add(job)
        db.session.commit()
        task_id = job.task_id
        
        # 3. Claim Job (Should Lock 5.00)
        print(f"[3] Agent Claiming Task {task_id} (Stake: 5.00)...")
        EscrowManager.stake_funds(agent_id, 5.00, task_id)
        job.claimed_by = agent_id
        job.status = 'claimed'
        db.session.commit()
        
        # Verify Stake
        agent = Agent.query.filter_by(agent_id=agent_id).first()
        print(f"    > Agent Balance: {agent.balance} (Expected 95.00)")
        print(f"    > Locked Balance: {agent.locked_balance} (Expected 5.00)")
        assert agent.balance == 95.00
        assert agent.locked_balance == 5.00
        
        # 4. Submit Job
        print("[4] Submitting Solution...")
        # Payload mimicking submission
        submission = {
            "content": "print('Hello')", # For Sandbox
            "webhook_payload": {"status": "ok"} # For Webhook (Pre-seeded? No, usually comes later)
        }
        # In real flow, Webhook comes via callback.
        # Let's simulate partial submission first (Sandbox only)
        # Sandbox passes (100 * 0.5) = 50. Webhook missing (0 * 0.5) = 0. Total 50.
        # Threshold 80. Fail?
        
        job.result_data = submission
        
        # Run Verification
        result = VerifierFactory.verify_composite(job, submission)
        print(f"    > Verification Result (Partial): Score={result['score']}")
        
        # 5. Simulate Webhook Callback
        print("[5] Simulating Webhook Callback...")
        # Update result data
        submission['webhook_payload'] = {"status": "ok"}
        job.result_data = submission
        db.session.commit()
        
        result = VerifierFactory.verify_composite(job, submission)
        print(f"    > Verification Result (Full): Score={result['score']}")
        assert result['score'] >= 80 # Should be 100
        
        # 6. Settle
        print("[6] Settling Job...")
        from server import _settle_job
        _settle_job(job, success=True)
        
        # Verify Balances
        db.session.refresh(agent)
        print(f"    > Final Agent Balance: {agent.balance}")
        # Initial 100 - 5 (Stake) + 5 (Return) + 40 (80% of 50 Reward) = 140
        print(f"    > Expected: 140.00")
        assert agent.balance == 140.00
        assert agent.locked_balance == 0
        
        print("=== TEST PASSED ===")

if __name__ == "__main__":
    test_v2_logic()
