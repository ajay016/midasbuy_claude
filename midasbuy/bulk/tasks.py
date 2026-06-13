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

_LOCK_TTL = 180  # seconds; per-item safety expiry so a crashed worker can't deadlock


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

    job.status = JobStatus.RUNNING
    job.started_at = timezone.now()
    job.celery_task_id = self.request.id or ""
    job.save(update_fields=["status", "started_at", "celery_task_id"])

    # The server rotates accounts per item now — just confirm at least one is up.
    from accounts.services.rotation import available_accounts

    if not available_accounts():
        job.status = JobStatus.FAILED
        job.error = "No logged-in Midasbuy account available."
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error", "finished_at"])
        _deliver_webhook(job)
        return {"job_id": job_id, "status": job.status}

    for item in job.items.filter(status=ItemStatus.PENDING).iterator():
        _process_item(job, item)

    job.refresh_from_db()
    job.status = JobStatus.COMPLETED
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "finished_at"])
    logger.info(
        "[BULK] job %s done: %s/%s succeeded",
        job_id, job.succeeded_items, job.total_items,
    )
    _deliver_webhook(job)
    return {
        "job_id": job_id,
        "status": job.status,
        "succeeded": job.succeeded_items,
        "failed": job.failed_items,
    }


def _process_item(job: BulkJob, item: BulkJobItem):
    """Pick a healthy account (rotating, respecting its per-minute cap), lock it
    for this item so two jobs can't share a session, run the operation, and feed
    the outcome back to the rotator for flagging."""
    from accounts.services.rotation import pick_account, report_result

    item.status = ItemStatus.PROCESSING
    item.save(update_fields=["status"])

    metered = job.job_type != JobType.PLAYER_INFO  # lookups don't count to the cap
    account_id = None
    out = {"success": False, "message": "No account available right now.", "raw": {}}

    # Try a few accounts so a momentarily-locked one doesn't stall the item.
    for _ in range(5):
        acct, reason = pick_account(metered=metered)
        if acct is None:
            out = {"success": False, "message": reason or out["message"], "raw": {}}
            break
        with _account_lock(acct.id) as got_lock:
            if not got_lock:
                continue  # busy — rotate to another account
            account_id = acct.id
            ssp, cookies = resolve_session(acct.id)
            if not ssp:
                out = {"success": False,
                       "message": "Selected account has no valid session.", "raw": {}}
                break
            try:
                if job.job_type == JobType.PLAYER_INFO:
                    out = process_player_info(item.player_id, job.country_code, ssp, cookies)
                elif job.job_type == JobType.VALIDATE:
                    out = process_validate(item.player_id, item.pin_code, job.country_code,
                                           ssp, cookies, item.zone_id)
                else:  # REDEEM
                    out = process_redeem(item.player_id, item.pin_code, job.country_code,
                                         ssp, cookies, item.zone_id)
            except Exception as exc:  # never let one bad item kill the whole job
                logger.exception("[BULK] item %s crashed", item.pk)
                out = {"success": False, "message": f"Internal error: {exc}", "raw": {}}
            break

    if account_id is not None:
        report_result(account_id, bool(out.get("success")))

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


def _deliver_webhook(job: BulkJob) -> None:
    """POST the finished job result to job.webhook_url with an HMAC signature.
    Best-effort: a webhook failure never fails the job."""
    if not job.webhook_url:
        return

    import json

    import httpx

    from apiauth.security import webhook_signature

    def _item(i):
        return {
            "id": i.id, "player_id": i.player_id, "pin_code": i.pin_code,
            "zone_id": i.zone_id, "status": i.status, "success": i.success,
            "message": i.message, "username": i.username, "product_name": i.product_name,
        }

    items = [_item(i) for i in job.items.order_by("id")]
    payload = {
        "job_id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "account_id": job.account_id,
        "country_code": job.country_code,
        "total_items": job.total_items,
        "succeeded_items": job.succeeded_items,
        "failed_items": job.failed_items,
        "error": job.error,
        "valid": [i for i in items if i["success"]],
        "invalid": [i for i in items if not i["success"]],
        "items": items,
    }
    body = json.dumps(payload, default=str).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": webhook_signature(body),
        "X-Webhook-Event": "bulk_job.completed",
    }
    try:
        resp = httpx.post(job.webhook_url, content=body, headers=headers, timeout=15.0)
        delivered = resp.status_code < 400
    except Exception:
        logger.exception("[BULK] webhook POST failed for job %s", job.id)
        delivered = False

    if delivered:
        BulkJob.objects.filter(pk=job.pk).update(webhook_delivered=True)
        logger.info("[BULK] webhook delivered for job %s", job.id)
