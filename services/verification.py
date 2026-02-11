"""
Orchestrates the CVS verification flow:
1. Runs VerifierFactory.verify_composite()
2. Computes evidence hash
3. Submits verdict to CVSOracle on-chain (if connected)
4. Calls SettlementService based on result
"""
import hashlib
import json
from core.verifier_factory import VerifierFactory
from services.settlement import SettlementService
from services.chain_bridge import get_chain_bridge
from models import db


class VerificationService:
    @staticmethod
    def verify_and_settle(job, submission: dict) -> dict:
        """
        Full CVS pipeline:
        1. Run composite verification
        2. Compute evidence hash from verification details
        3. Submit verdict on-chain (if chain_bridge connected)
        4. Settle or reject via SettlementService
        Returns combined result dict.
        """
        # Step 1: Run verifiers
        verification_result = VerifierFactory.verify_composite(job, submission)
        score = verification_result['score']
        accepted = verification_result['success']

        # Step 2: Compute evidence hash
        evidence_str = json.dumps(verification_result, sort_keys=True, default=str)
        evidence_hash = bytes.fromhex(
            hashlib.sha256(evidence_str.encode()).hexdigest()
        )

        # Step 3: Submit verdict on-chain
        verdict_tx = None
        bridge = get_chain_bridge()
        if bridge.is_connected() and job.chain_task_id:
            try:
                verdict_tx = bridge.submit_verdict(
                    chain_task_id=job.chain_task_id,
                    accepted=accepted,
                    score=min(int(score), 255),  # uint8
                    evidence_hash=evidence_hash,
                )
            except Exception as e:
                print(f"[Verification] On-chain verdict failed: {e}")

        # Step 4: Store verdict data on job
        job.verdict_data = {
            "score": score,
            "accepted": accepted,
            "evidence_hash": evidence_hash.hex(),
            "details": verification_result.get('reason', ''),
            "verdict_tx": verdict_tx,
        }

        # Step 5: Settle
        if accepted:
            # On-chain settle (if connected)
            if bridge.is_connected() and job.chain_task_id:
                try:
                    bridge.settle(job.chain_task_id, bridge.oracle_private_key)
                except Exception as e:
                    print(f"[Verification] On-chain settle failed: {e}")

            settlement = SettlementService.settle_success(job)
            return {
                "status": "settled",
                "verification": verification_result,
                "settlement": settlement,
                "verdict_tx": verdict_tx,
            }
        else:
            settlement = SettlementService.settle_reject(job)
            return {
                "status": settlement.get("status", "rejected"),
                "message": "Verification Failed",
                "verification": verification_result,
                "settlement": settlement,
                "verdict_tx": verdict_tx,
            }
