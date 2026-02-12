from models import db, Agent, Job, Submission, JobParticipant
from datetime import datetime, timezone


# Expirable states (funded tasks that haven't resolved)
_EXPIRABLE_STATUSES = ('funded',)


class JobService:
    @staticmethod
    def check_expiry(job: Job) -> bool:
        """Lazy expiry check. Returns True if task was just expired."""
        if job.status not in _EXPIRABLE_STATUSES:
            return False
        if not job.expiry:
            return False
        now = datetime.now(timezone.utc)
        exp = job.expiry if job.expiry.tzinfo else job.expiry.replace(tzinfo=timezone.utc)
        if now >= exp:
            job.status = 'expired'
            # F09: Only cancel pending submissions; let judging submissions
            # be handled by the oracle timeout mechanism (G07)
            Submission.query.filter(
                Submission.task_id == job.task_id,
                Submission.status == 'pending',
            ).update({'status': 'failed'}, synchronize_session='fetch')
            db.session.flush()
            return True
        return False

    @staticmethod
    def list_jobs(status=None, buyer_id=None, worker_id=None,
                  artifact_type=None, min_price=None, max_price=None,
                  sort_by='created_at', sort_order='desc',
                  limit=50, offset=0):
        """G03: Enhanced job listing with filtering, sorting, pagination."""
        from decimal import Decimal, InvalidOperation

        query = Job.query
        if status:
            query = query.filter(Job.status == status)
        if buyer_id:
            query = query.filter(Job.buyer_id == buyer_id)
        if artifact_type:
            query = query.filter(Job.artifact_type == artifact_type)
        if min_price is not None:
            try:
                query = query.filter(Job.price >= Decimal(str(min_price)))
            except (InvalidOperation, ValueError):
                pass
        if max_price is not None:
            try:
                query = query.filter(Job.price <= Decimal(str(max_price)))
            except (InvalidOperation, ValueError):
                pass

        # M2 fix: SQL-level safety cap to prevent unbounded memory usage.
        # Expiry + worker filtering still done in-memory, but capped at 5000 rows.
        all_jobs = query.order_by(Job.created_at.desc()).limit(5000).all()

        # Lazy expiry check on listed jobs
        any_expired = False
        for job in all_jobs:
            if JobService.check_expiry(job):
                any_expired = True
        if any_expired:
            db.session.commit()
        # Re-filter if status was specified (some may have just expired)
        if status:
            all_jobs = [j for j in all_jobs if j.status == status]
        # Python-level worker filter using JobParticipant (portable across DB engines)
        if worker_id:
            participant_task_ids = {jp.task_id for jp in JobParticipant.query.filter_by(worker_id=worker_id, unclaimed_at=None).all()}
            all_jobs = [j for j in all_jobs if j.task_id in participant_task_ids]

        total = len(all_jobs)

        # Sort (m7 fix: type-appropriate defaults to avoid Decimal/datetime mix)
        from decimal import Decimal as _Dec
        sort_col_map = {'created_at': 'created_at', 'price': 'price', 'expiry': 'expiry'}
        _sort_defaults = {'created_at': datetime.min, 'expiry': datetime.min, 'price': _Dec(0)}
        sort_key = sort_col_map.get(sort_by, 'created_at')
        sort_default = _sort_defaults.get(sort_key, datetime.min)
        reverse = sort_order != 'asc'
        all_jobs.sort(
            key=lambda j: getattr(j, sort_key) if getattr(j, sort_key) is not None else sort_default,
            reverse=reverse,
        )

        # Clamp limit
        limit = min(max(1, limit), 200)
        offset = max(0, offset)

        paginated = all_jobs[offset:offset + limit]
        return paginated, total

    @staticmethod
    def get_job(task_id: str) -> Job:
        job = Job.query.filter_by(task_id=task_id).first()
        if job:
            if JobService.check_expiry(job):
                db.session.commit()
        return job

    @staticmethod
    def to_dict(job: Job) -> dict:
        submission_count = Submission.query.filter_by(task_id=job.task_id).count()
        participants_query = JobParticipant.query.filter_by(task_id=job.task_id, unclaimed_at=None).all()
        if participants_query:
            agent_names = {a.agent_id: a.name for a in Agent.query.filter(
                Agent.agent_id.in_([jp.worker_id for jp in participants_query])
            ).all()}
        else:
            agent_names = {}
        return {
            "task_id": job.task_id,
            "title": job.title,
            "description": job.description,
            "rubric": job.rubric,
            "price": float(job.price),
            "buyer_id": job.buyer_id,
            "status": job.status,
            "artifact_type": job.artifact_type,
            "participants": [{"agent_id": jp.worker_id, "name": agent_names.get(jp.worker_id, jp.worker_id)} for jp in participants_query],
            "winner_id": job.winner_id,
            "submission_count": submission_count,
            "max_submissions": job.max_submissions,
            "max_retries": job.max_retries,
            "min_reputation": float(job.min_reputation) if job.min_reputation else None,
            "expiry": job.expiry.isoformat() if job.expiry else None,
            "deposit_tx_hash": job.deposit_tx_hash,
            "payout_tx_hash": job.payout_tx_hash,
            "payout_status": job.payout_status,
            "fee_tx_hash": job.fee_tx_hash,
            "fee_bps": job.fee_bps,
            "depositor_address": job.depositor_address,
            "failure_count": job.failure_count or 0,
            "deposit_amount": float(job.deposit_amount) if job.deposit_amount else None,
            "refund_tx_hash": job.refund_tx_hash,
            "solution_price": float(job.solution_price or 0),
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        }
