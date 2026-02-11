from models import db, Job, Submission
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
            # Cancel any pending/judging submissions
            Submission.query.filter(
                Submission.task_id == job.task_id,
                Submission.status.in_(['pending', 'judging']),
            ).update({'status': 'failed'}, synchronize_session='fetch')
            db.session.flush()
            return True
        return False

    @staticmethod
    def list_jobs(status=None, buyer_id=None, worker_id=None):
        query = Job.query
        if status:
            query = query.filter(Job.status == status)
        if buyer_id:
            query = query.filter(Job.buyer_id == buyer_id)
        jobs = query.order_by(Job.created_at.desc()).all()
        # Lazy expiry check on listed jobs
        any_expired = False
        for job in jobs:
            if JobService.check_expiry(job):
                any_expired = True
        # L10: Persist expiry changes from read path
        if any_expired:
            db.session.commit()
        # Re-filter if status was specified (some may have just expired)
        if status:
            jobs = [j for j in jobs if j.status == status]
        # Python-level worker filter (portable across DB engines)
        if worker_id:
            jobs = [j for j in jobs if worker_id in (j.participants or [])]
        return jobs

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
        return {
            "task_id": job.task_id,
            "title": job.title,
            "description": job.description,
            "rubric": job.rubric,
            "price": float(job.price),
            "buyer_id": job.buyer_id,
            "status": job.status,
            "artifact_type": job.artifact_type,
            "participants": job.participants or [],
            "winner_id": job.winner_id,
            "submission_count": submission_count,
            "max_submissions": job.max_submissions,
            "max_retries": job.max_retries,
            "min_reputation": float(job.min_reputation) if job.min_reputation else None,
            "expiry": job.expiry.isoformat() if job.expiry else None,
            "deposit_tx_hash": job.deposit_tx_hash,
            "payout_tx_hash": job.payout_tx_hash,
            "fee_tx_hash": job.fee_tx_hash,
            "depositor_address": job.depositor_address,
            "refund_tx_hash": job.refund_tx_hash,
            "solution_price": float(job.solution_price or 0),
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        }
