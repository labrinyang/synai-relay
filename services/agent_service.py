import re as _re
from models import db, Agent, Job, Submission, JobParticipant


class AgentService:
    @staticmethod
    def register(agent_id: str, name: str = None, wallet_address: str = None) -> dict:
        if wallet_address and not _re.match(r'^0x[0-9a-fA-F]{40}$', wallet_address):
            return {"error": "Invalid wallet address format"}

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
        # Count tasks where this agent is a participant (claimed)
        total_claims = JobParticipant.query.filter_by(worker_id=agent_id).count()
        passed = db.session.query(db.func.count(Submission.id)).filter(
            Submission.worker_id == agent_id,
            Submission.status == 'passed',
        ).scalar() or 0

        if total_claims > 0:
            agent.completion_rate = passed / total_claims
        else:
            agent.completion_rate = None

        # Update metrics.reliability
        metrics = dict(agent.metrics or {"engineering": 0, "creativity": 0, "reliability": 0})
        metrics['reliability'] = passed - (total_claims - passed)
        agent.metrics = metrics

        db.session.flush()

    @staticmethod
    def rotate_api_key(agent_id: str) -> dict:
        """Generate a new API key, invalidating the old one."""
        from services.auth_service import generate_api_key
        agent = Agent.query.filter_by(agent_id=agent_id).first()
        if not agent:
            return {"error": "Agent not found"}
        raw_key, key_hash = generate_api_key()
        agent.api_key_hash = key_hash
        db.session.commit()
        return {"agent_id": agent_id, "api_key": raw_key}

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
