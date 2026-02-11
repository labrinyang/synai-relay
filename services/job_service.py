from models import db, Job
from datetime import datetime, timezone


_EXPIRABLE_STATUSES = ('funded', 'claimed', 'submitted', 'rejected')


class JobService:
    @staticmethod
    def check_expiry(job):
        """
        Lazy expiry check. If task has an expiry and current time
        exceeds it, and the task is in an expirable state, mark it expired.
        Returns True if the task was just expired.
        """
        if not job.expiry:
            return False

        if datetime.utcnow() > job.expiry:
            if job.status in _EXPIRABLE_STATUSES:
                job.status = 'expired'
                db.session.commit()
                return True
            if job.status == 'created':
                job.status = 'cancelled'
                db.session.commit()
                return True
        return False

    @staticmethod
    def list_jobs(status=None, buyer_id=None, claimed_by=None):
        """
        List jobs with optional filters.
        Runs lazy expiry check (batched) on each result.
        """
        query = Job.query

        if status:
            query = query.filter(Job.status == status)
        if buyer_id:
            query = query.filter(Job.buyer_id == buyer_id)
        if claimed_by:
            query = query.filter(Job.claimed_by == claimed_by)

        jobs = query.order_by(Job.created_at.desc()).all()

        # Batch lazy expiry — mark all expired first, then single commit
        now = datetime.utcnow()
        changed_any = False
        for j in jobs:
            if j.expiry and now > j.expiry:
                if j.status in _EXPIRABLE_STATUSES:
                    j.status = 'expired'
                    changed_any = True
                elif j.status == 'created':
                    j.status = 'cancelled'
                    changed_any = True
        if changed_any:
            db.session.commit()

        return jobs

    @staticmethod
    def get_job(task_id):
        """
        Get a single job by task_id. Runs lazy expiry check.
        Returns None if not found.
        """
        job = Job.query.filter_by(task_id=task_id).first()
        if job:
            JobService.check_expiry(job)
        return job

    @staticmethod
    def to_dict(job):
        """Standard job serialization."""
        return {
            "task_id": str(job.task_id),
            "title": job.title,
            "description": job.description,
            "price": float(job.price),
            "status": job.status,
            "buyer_id": job.buyer_id,
            "claimed_by": job.claimed_by,
            "artifact_type": job.artifact_type,
            "deposit_amount": float(job.deposit_amount) if job.deposit_amount else 0,
            "verifiers_config": job.verifiers_config,
            "result_data": job.result_data,
            "failure_count": job.failure_count,
            "max_retries": job.max_retries or 3,
            "expiry": job.expiry.isoformat() if job.expiry else None,
            "chain_task_id": job.chain_task_id,
            "verdict_data": job.verdict_data,
            "created_at": job.created_at.isoformat() if job.created_at else None,
        }
