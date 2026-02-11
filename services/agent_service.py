from models import db, Agent, Submission


class AgentService:
    @staticmethod
    def register(agent_id: str, name: str = None, wallet_address: str = None) -> dict:
        existing = Agent.query.filter_by(agent_id=agent_id).first()
        if existing:
            return {"error": "Agent already registered", "agent_id": agent_id}

        agent = Agent(
            agent_id=agent_id,
            name=name or agent_id,
            wallet_address=wallet_address,
        )
        db.session.add(agent)
        db.session.commit()
        return {
            "agent_id": agent_id,
            "name": agent.name,
            "wallet_address": wallet_address,
        }

    @staticmethod
    def get_profile(agent_id: str) -> dict:
        agent = Agent.query.filter_by(agent_id=agent_id).first()
        if not agent:
            return None
        return AgentService._to_dict(agent)

    @staticmethod
    def update_reputation(agent_id: str):
        """Recalculate completion_rate from submission history."""
        agent = Agent.query.filter_by(agent_id=agent_id).first()
        if not agent:
            return
        total_claims = db.session.query(db.func.count(db.distinct(Submission.task_id))).filter(
            Submission.worker_id == agent_id
        ).scalar() or 0
        passed = db.session.query(db.func.count(Submission.id)).filter(
            Submission.worker_id == agent_id,
            Submission.status == 'passed',
        ).scalar() or 0

        if total_claims > 0:
            agent.completion_rate = passed / total_claims
        else:
            agent.completion_rate = None
        db.session.flush()

    @staticmethod
    def _to_dict(agent: Agent) -> dict:
        return {
            "agent_id": agent.agent_id,
            "name": agent.name,
            "owner_id": agent.owner_id,
            "wallet_address": agent.wallet_address,
            "metrics": agent.metrics or {},
            "completion_rate": float(agent.completion_rate) if agent.completion_rate is not None else None,
            "total_earned": float(agent.total_earned or 0),
            "adopted_at": agent.adopted_at.isoformat() if agent.adopted_at else None,
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
        }
