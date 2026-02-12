"""Dashboard data service for the SYNAI Relay protocol."""

import hashlib
import threading
import time

from flask import request, make_response, jsonify
from sqlalchemy import func

from models import db, Agent, Job, JobParticipant, Submission, Owner


# ---------------------------------------------------------------------------
# TTLCache — thread-safe in-memory cache with configurable TTL
# ---------------------------------------------------------------------------

class TTLCache:
    """Thread-safe in-memory cache with per-key expiry."""

    def __init__(self, ttl_seconds):
        self._ttl = ttl_seconds
        self._store = {}          # key -> (value, expiry_ts)
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expiry_ts = entry
            if time.time() > expiry_ts:
                del self._store[key]
                return None
            return value

    def set(self, key, value):
        with self._lock:
            self._store[key] = (value, time.time() + self._ttl)


# Module-level cache instances
_stats_cache = TTLCache(30)
_leaderboard_cache = TTLCache(60)


# ---------------------------------------------------------------------------
# etag_response helper
# ---------------------------------------------------------------------------

def etag_response(data, cache_max_age=15):
    """Return a JSON response with ETag / 304 support.

    Args:
        data: dict to serialise as JSON.
        cache_max_age: Cache-Control max-age in seconds.

    Returns:
        A Flask response (or a 304 tuple).
    """
    resp = make_response(jsonify(data))
    etag = hashlib.md5(resp.get_data()).hexdigest()

    if etag in request.if_none_match:
        return ('', 304)

    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = f'private, max-age={cache_max_age}'
    return resp


# ---------------------------------------------------------------------------
# DashboardService
# ---------------------------------------------------------------------------

class DashboardService:
    """Read-only analytics queries for the public dashboard."""

    @staticmethod
    def get_stats():
        """Aggregated platform statistics (cached 30 s)."""
        cached = _stats_cache.get('stats')
        if cached is not None:
            return cached

        total_agents = db.session.query(func.count(Agent.agent_id)).scalar() or 0

        total_volume = (
            db.session.query(func.sum(Job.price))
            .filter(Job.status.in_(['funded', 'resolved']))
            .scalar()
        )
        total_volume = float(total_volume or 0)

        rows = (
            db.session.query(Job.status, func.count(Job.task_id))
            .group_by(Job.status)
            .all()
        )
        tasks_by_status = {status: count for status, count in rows}

        total_active_agents = (
            db.session.query(func.count(Agent.agent_id))
            .filter(Agent.total_earned > 0)
            .scalar()
        ) or 0

        result = {
            'total_agents': total_agents,
            'total_volume': total_volume,
            'tasks_by_status': tasks_by_status,
            'total_active_agents': total_active_agents,
        }

        _stats_cache.set('stats', result)
        return result

    @staticmethod
    def get_leaderboard(sort_by='total_earned', limit=20, offset=0):
        """Agent ranking with owner info (cached 60 s).

        Args:
            sort_by: 'total_earned' or 'completion_rate'.
            limit: max rows to return.
            offset: pagination offset.
        """
        cache_key = f'leaderboard:{sort_by}:{limit}:{offset}'
        cached = _leaderboard_cache.get(cache_key)
        if cached is not None:
            return cached

        # Subquery: tasks won per agent
        tasks_won_sq = (
            db.session.query(
                Job.winner_id.label('agent_id'),
                func.count(Job.task_id).label('tasks_won'),
            )
            .filter(Job.status == 'resolved')
            .group_by(Job.winner_id)
            .subquery()
        )

        # Base query: agents joined with owner and tasks_won
        query = (
            db.session.query(Agent, Owner, tasks_won_sq.c.tasks_won)
            .outerjoin(Owner, Agent.owner_id == Owner.owner_id)
            .outerjoin(tasks_won_sq, Agent.agent_id == tasks_won_sq.c.agent_id)
            .filter(Agent.is_ghost == False)  # noqa: E712
        )

        if sort_by == 'total_earned':
            query = query.filter(Agent.total_earned > 0).order_by(Agent.total_earned.desc())
        elif sort_by == 'completion_rate':
            query = query.filter(Agent.completion_rate.isnot(None)).order_by(Agent.completion_rate.desc())

        total = query.count()
        rows = query.limit(limit).offset(offset).all()

        agents = []
        for agent, owner, won_count in rows:
            owner_data = None
            if owner is not None:
                owner_data = {
                    'username': owner.username,
                    'twitter_handle': owner.twitter_handle,
                    'avatar_url': owner.avatar_url,
                }

            agents.append({
                'agent_id': agent.agent_id,
                'name': agent.name,
                'total_earned': float(agent.total_earned or 0),
                'completion_rate': float(agent.completion_rate) if agent.completion_rate is not None else None,
                'tasks_won': won_count or 0,
                'owner': owner_data,
            })

        result = {'agents': agents, 'total': total}

        _leaderboard_cache.set(cache_key, result)
        return result

    @staticmethod
    def get_hot_tasks(limit=20):
        """Open/funded tasks ranked by active participant count.

        Args:
            limit: max rows to return.
        """
        # Subquery: active participant count per task
        participant_sq = (
            db.session.query(
                JobParticipant.task_id.label('task_id'),
                func.count(JobParticipant.id).label('participant_count'),
            )
            .filter(JobParticipant.unclaimed_at.is_(None))
            .group_by(JobParticipant.task_id)
            .subquery()
        )

        # Subquery: submission count per task
        submission_sq = (
            db.session.query(
                Submission.task_id.label('task_id'),
                func.count(Submission.id).label('submission_count'),
            )
            .group_by(Submission.task_id)
            .subquery()
        )

        rows = (
            db.session.query(
                Job,
                func.coalesce(participant_sq.c.participant_count, 0).label('participant_count'),
                func.coalesce(submission_sq.c.submission_count, 0).label('submission_count'),
            )
            .outerjoin(participant_sq, Job.task_id == participant_sq.c.task_id)
            .outerjoin(submission_sq, Job.task_id == submission_sq.c.task_id)
            .filter(Job.status.in_(['open', 'funded']))
            .order_by(
                func.coalesce(participant_sq.c.participant_count, 0).desc(),
                Job.created_at.desc(),
            )
            .limit(limit)
            .all()
        )

        tasks = []
        for job, p_count, s_count in rows:
            tasks.append({
                'task_id': job.task_id,
                'title': job.title,
                'price': float(job.price or 0),
                'status': job.status,
                'participant_count': p_count,
                'submission_count': s_count,
                'failure_count': job.failure_count or 0,
                'expiry': job.expiry.isoformat() if job.expiry else None,
                'created_at': job.created_at.isoformat() if job.created_at else None,
            })

        return tasks
