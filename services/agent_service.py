from models import db, Agent, LedgerEntry
from wallet_manager import wallet_manager
from decimal import Decimal


class AgentService:
    @staticmethod
    def register(agent_id: str, name: str = None) -> dict:
        """
        Register a new agent with a managed wallet.
        Returns agent dict or raises ValueError if already exists.
        """
        existing = Agent.query.filter_by(agent_id=agent_id).first()
        if existing:
            raise ValueError(f"Agent '{agent_id}' already registered")

        addr, enc_key = wallet_manager.create_wallet()
        agent = Agent(
            agent_id=agent_id,
            name=name or f"Agent_{agent_id[:8]}",
            balance=Decimal('0'),
            locked_balance=Decimal('0'),
            wallet_address=addr,
            encrypted_privkey=enc_key
        )
        db.session.add(agent)
        db.session.commit()
        return AgentService._to_dict(agent)

    @staticmethod
    def get_or_create(agent_id: str) -> Agent:
        """
        Get existing agent or auto-register (fallback for deposit).
        Returns the Agent model instance.
        """
        agent = Agent.query.filter_by(agent_id=agent_id).first()
        if not agent:
            addr, enc_key = wallet_manager.create_wallet()
            agent = Agent(
                agent_id=agent_id,
                name=f"Agent_{agent_id[:8]}",
                balance=Decimal('0'),
                locked_balance=Decimal('0'),
                wallet_address=addr,
                encrypted_privkey=enc_key
            )
            db.session.add(agent)
            db.session.flush()
        return agent

    @staticmethod
    def get_profile(agent_id: str) -> dict:
        """
        Return full agent profile including balance, locked_balance,
        metrics, wallet address.
        """
        agent = Agent.query.filter_by(agent_id=agent_id).first()
        if not agent:
            return None
        return AgentService._to_dict(agent)

    @staticmethod
    def deposit(agent_id: str, amount: Decimal) -> dict:
        """
        Deposit funds. Auto-registers agent if not found.
        Returns updated profile dict.
        """
        agent = AgentService.get_or_create(agent_id)
        agent.balance += amount
        entry = LedgerEntry(
            source_id='deposit',
            target_id=agent_id,
            amount=amount,
            transaction_type='deposit',
            task_id=None
        )
        db.session.add(entry)
        db.session.commit()
        return AgentService._to_dict(agent)

    @staticmethod
    def _to_dict(agent) -> dict:
        return {
            "agent_id": agent.agent_id,
            "name": agent.name,
            "balance": str(agent.balance),
            "locked_balance": str(agent.locked_balance or 0),
            "wallet_address": agent.wallet_address,
            "metrics": agent.metrics or {"engineering": 0, "creativity": 0, "reliability": 0},
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
        }
