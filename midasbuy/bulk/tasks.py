"""
Celery tasks for bulk operations.

One task processes one BulkJob. Items inside a job run sequentially so they
reuse the same warm browser session (fast) and never fight over it. A per-account
Redis lock makes sure two jobs for the *same* account don't run at the same time
(which would corrupt that account's session); jobs for *different* accounts still
run in parallel across worker slots.
"""
import contextlib
import logging
import uuid

from celery import shared_task
from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import BulkJob, BulkJobItem, ItemStatus, JobStatus, JobType
from .services import (
    process_player_info,
    process_redeem,
    process_validate,
    resolve_session,
)

logger = logging.getLogger("bulk")

_LOCK_TTL = 60 * 60  # seconds; safety expiry so a crashed worker can't deadlock


@contextlib.contextmanager
def _account_lock(account_id):
    """Best-effort distributed lock keyed on the account (Redis SET NX EX)."""
    if account_id is None:
        yield True
        return

    import redis

    client = redis.from_url(settings.CELERY_BROKER_URL)
    key = f"bulk:account-lock:{account_id}"
    token = str(uuid.uuid4())
    acquired = client.set(key, token, nx=True, ex=_LOCK_TTL)
    try:
        yield bool(acquired)
    finally:
        if acquired:
            # Release only if we still own it (avoid deleting someone else's lock).
            if client.get(key) == token.encode():
                client.delete(key)


@shared_task(bind=True, max_retries=None)
def run_bulk_job(self, job_id: int):
    job = BulkJob.objects.get(pk=job_id)

    with _account_lock(job.account_id) as got_lock:
        if not got_lock:
            # Another job for this account is running — try again shortly.
            logger.info("[BULK] account %s busy; retrying job %s in 30s", job.account_id, job_id)
            raise self.retry(countdown=30)

        job.status = JobStatus.RUNNING
        job.started_at = timezone.now()
        job.celery_task_id = self.request.id or ""
        job.save(update_fields=["status", "started_at", "celery_task_id"])

        ssp, cookies = resolve_session(job.account_id)
        if not ssp:
            job.status = JobStatus.FAILED
            job.error = "No valid session for this account. Log the account in first."
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error", "finished_at"])
            return {"job_id": job_id, "status": job.status}

        for item in job.items.filter(status=ItemStatus.PENDING).iterator():
            _process_item(job, item, ssp, cookies)

        job.refresh_from_db()
        job.status = JobStatus.COMPLETED
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
        logger.info(
            "[BULK] job %s done: %s/%s succeeded",
            job_id, job.succeeded_items, job.total_items,
        )
        return {
            "job_id": job_id,
            "status": job.status,
            "succeeded": job.succeeded_items,
            "failed": job.failed_items,
        }


def _process_item(job: BulkJob, item: BulkJobItem, ssp: str, cookies: str):
    item.status = ItemStatus.PROCESSING
    item.save(update_fields=["status"])

    try:
        if job.job_type == JobType.PLAYER_INFO:
            out = process_player_info(item.player_id, job.country_code, ssp, cookies)
        elif job.job_type == JobType.VALIDATE:
            out = process_validate(
                item.player_id, item.pin_code, job.country_code, ssp, cookies, item.zone_id
            )
        else:  # REDEEM
            out = process_redeem(
                item.player_id, item.pin_code, job.country_code, ssp, cookies, item.zone_id
            )
    except Exception as exc:  # never let one bad item kill the whole job
        logger.exception("[BULK] item %s crashed", item.pk)
        out = {"success": False, "message": f"Internal error: {exc}", "raw": {}}

    item.success = bool(out.get("success"))
    item.status = ItemStatus.SUCCESS if item.success else ItemStatus.FAILED
    item.message = out.get("message", "")
    item.username = out.get("username", "") or item.username
    item.product_name = out.get("product_name", "") or item.product_name
    if out.get("zone_id"):
        item.zone_id = str(out["zone_id"])
    item.result = out.get("raw", {}) or {}
    item.processed_at = timezone.now()
    item.save()

    # Update the job's denormalised counters atomically.
    BulkJob.objects.filter(pk=job.pk).update(
        processed_items=F("processed_items") + 1,
        succeeded_items=F("succeeded_items") + (1 if item.success else 0),
        failed_items=F("failed_items") + (0 if item.success else 1),
    )
