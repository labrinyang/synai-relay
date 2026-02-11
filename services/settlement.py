from models import db, Agent, Job, LedgerEntry
from core.escrow_manager import EscrowManager
from decimal import Decimal


class SettlementService:
    PLATFORM_FEE_RATE = Decimal('0.20')  # 20%
    WORKER_RATE = Decimal('0.80')        # 80%

    @staticmethod
    def settle_success(job: Job) -> dict:
        """
        Called when CVS accepts the result.
        Worker gets 80%, platform gets 20%, full stake returned.
        Sets job status to 'settled'.
        """
        if job.status in ('settled', 'refunded'):
            return {"error": "Already settled"}

        agent_id = job.claimed_by
        agent = Agent.query.filter_by(agent_id=agent_id).first()

        price = job.price
        platform_fee = price * SettlementService.PLATFORM_FEE_RATE
        seller_payout = price * SettlementService.WORKER_RATE

        if agent:
            agent.balance += seller_payout

            db.session.add(LedgerEntry(
                source_id='platform', target_id=agent_id,
                amount=seller_payout, transaction_type='task_payout',
                task_id=job.task_id
            ))
            db.session.add(LedgerEntry(
                source_id='platform', target_id='platform_admin',
                amount=platform_fee, transaction_type='platform_fee',
                task_id=job.task_id
            ))

            # Release full stake
            stake = job.deposit_amount or Decimal('0')
            if stake > 0:
                EscrowManager.release_stake(agent_id, stake, job.task_id)

            # Reputation boost
            metrics = agent.metrics or {"engineering": 0, "creativity": 0, "reliability": 0}
            metrics['reliability'] = metrics.get('reliability', 0) + 1
            agent.metrics = metrics

        job.status = 'settled'
        db.session.commit()

        return {
            "payout": float(seller_payout),
            "fee": float(platform_fee),
            "stake_return": float(job.deposit_amount or 0)
        }

    @staticmethod
    def settle_reject(job: Job) -> dict:
        """
        Called when CVS rejects the result.
        Full stake returned (no penalty). Job status -> 'rejected'.
        failure_count incremented. If failure_count >= max_retries -> 'expired'.
        """
        if job.status in ('settled', 'refunded'):
            return {"error": "Already settled"}

        agent_id = job.claimed_by
        agent = Agent.query.filter_by(agent_id=agent_id).first()

        stake = job.deposit_amount or Decimal('0')
        if agent and stake > 0:
            EscrowManager.release_stake(agent_id, stake, job.task_id)

            # Reputation dip
            metrics = agent.metrics or {"engineering": 0, "creativity": 0, "reliability": 0}
            metrics['reliability'] = max(0, metrics.get('reliability', 0) - 1)
            agent.metrics = metrics

        job.status = 'rejected'
        job.failure_count = (job.failure_count or 0) + 1

        max_retries = job.max_retries or 3
        if job.failure_count >= max_retries:
            job.status = 'expired'

        db.session.commit()

        return {
            "payout": 0,
            "fee": 0,
            "stake_return": float(stake),
            "failure_count": job.failure_count,
            "status": job.status,
        }
