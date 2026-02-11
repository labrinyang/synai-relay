from models import db, Agent, LedgerEntry
from decimal import Decimal

class EscrowManager:
    @staticmethod
    def stake_funds(agent_id, amount, task_id):
        """
        Locks funds from agent balance to locked_balance.
        """
        agent = Agent.query.filter_by(agent_id=agent_id).first()
        if not agent:
            raise ValueError("Agent not found")
            
        amount = Decimal(str(amount))
        
        if agent.balance < amount:
            raise ValueError(f"Insufficient funds for stake. Required: {amount}, Available: {agent.balance}")
            
        agent.balance -= amount
        agent.locked_balance += amount
        
        entry = LedgerEntry(
            source_id=agent_id, target_id='escrow_lock',
            amount=amount, transaction_type='stake_lock', task_id=task_id
        )
        db.session.add(entry)
        db.session.flush()
        return True

    @staticmethod
    def release_stake(agent_id, amount, task_id):
        """
        Returns locked funds to agent balance.
        """
        agent = Agent.query.filter_by(agent_id=agent_id).first()
        amount = Decimal(str(amount))
        
        if agent.locked_balance < amount:
            # Should not happen in normal flow
            amount = agent.locked_balance 
        
        agent.locked_balance -= amount
        agent.balance += amount
        
        entry = LedgerEntry(
            source_id='escrow_lock', target_id=agent_id,
            amount=amount, transaction_type='stake_release', task_id=task_id
        )
        db.session.add(entry)
        db.session.flush()

