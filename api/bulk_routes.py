"""
Bulk operation endpoints (mounted under /api/bulk).

Non-blocking: each endpoint creates a job + its rows, hands the job id to Celery,
and returns immediately. The browser work happens in a worker. Clients get the
result two ways:
  * polling   GET /api/bulk/jobs/{id}        (used by the panel)
  * webhook   set webhook_url on the request (the finished result is POSTed, signed)
"""
import logging

from asgiref.sync import sync_to_async
from fastapi import APIRouter, Depends, HTTPException, Query

from .bulk_schemas import (
    BulkCodeRequest,
    BulkJobDetailResponse,
    BulkJobItemResponse,
    BulkJobResponse,
    BulkPlayerInfoRequest,
)
from .dependencies import charge, require_order

logger = logging.getLogger("api")
# Every /bulk/* route requires authentication AND the order capability
# (JWT or signed request from a user permitted to place orders).
router = APIRouter(prefix="/bulk", tags=["bulk"], dependencies=[Depends(require_order)])


# ── Sync DB helpers (Django ORM is sync; call via sync_to_async) ───────────────
def _job_to_dict(job) -> dict:
    return {
        "job_id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "account_id": job.account_id,
        "country_code": job.country_code,
        "total_items": job.total_items,
        "processed_items": job.processed_items,
        "succeeded_items": job.succeeded_items,
        "failed_items": job.failed_items,
        "progress_percent": job.progress_percent,
        "webhook_url": job.webhook_url,
        "created_at": job.created_at,
    }


def _item_to_dict(item) -> dict:
    return {
        "id": item.id,
        "player_id": item.player_id,
        "pin_code": item.pin_code,
        "zone_id": item.zone_id,
        "status": item.status,
        "success": item.success,
        "message": item.message,
        "username": item.username,
        "product_name": item.product_name,
    }


def _account_ok(account_id: int) -> bool:
    from accounts.models import MidasbuyAccount

    return MidasbuyAccount.objects.filter(pk=account_id).exists()


def _create_job(job_type: str, account_id: int, country_code: str,
                rows: list[dict], webhook_url: str = "", owner_id: int | None = None) -> dict:
    """Create the job + items and enqueue the Celery task. Returns the job dict."""
    from bulk.models import BulkJob, BulkJobItem
    from bulk.tasks import run_bulk_job

    job = BulkJob.objects.create(
        job_type=job_type,
        requested_by_id=owner_id,
        account_id=account_id,
        country_code=country_code,
        total_items=len(rows),
        webhook_url=webhook_url or "",
    )
    BulkJobItem.objects.bulk_create([
        BulkJobItem(
            job=job,
            player_id=r["player_id"],
            pin_code=r.get("pin_code", ""),
            zone_id=r.get("zone_id", "1"),
        )
        for r in rows
    ])
    run_bulk_job.delay(job.id)
    return _job_to_dict(job)


def _owner_id(identity: dict) -> int:
    return identity.get("billing_user_id", identity["user_id"])


def _visible_jobs(identity: dict):
    """Jobs the caller may read: their own (as the billing user), their clients'
    if they're a partner, everything for admins. Jobs created before ownership was
    recorded (requested_by NULL) are admin-only. Without this, any user could read
    any job's pin codes by guessing sequential ids."""
    from apiauth.models import User
    from bulk.models import BulkJob
    from codes.services import owner_scope_q

    viewer = User.objects.get(pk=_owner_id(identity))
    return BulkJob.objects.filter(owner_scope_q(viewer, "requested_by"))


def _get_job_detail(identity: dict, job_id: int):
    """Job dict + valid/invalid split + items, in one DB round-trip set."""
    from bulk.models import BulkJob, BulkJobItem

    try:
        job = _visible_jobs(identity).get(pk=job_id)
    except BulkJob.DoesNotExist:
        return None
    items = [_item_to_dict(i) for i in BulkJobItem.objects.filter(job_id=job_id).order_by("id")]
    valid = [i for i in items if i["success"]]
    invalid = [i for i in items if not i["success"]]
    return {**_job_to_dict(job), "items": items, "valid": valid, "invalid": invalid}


def _get_items(identity: dict, job_id: int, limit: int, offset: int):
    from bulk.models import BulkJobItem

    if not _visible_jobs(identity).filter(pk=job_id).exists():
        return None
    qs = BulkJobItem.objects.filter(job_id=job_id).order_by("id")[offset:offset + limit]
    return [_item_to_dict(i) for i in qs]


# ── Endpoints ──────────────────────────────────────────────────────────────────
@router.post("/player-info", response_model=BulkJobResponse,
             summary="Bulk player lookup")
async def bulk_player_info(body: BulkPlayerInfoRequest,
                           identity: dict = Depends(require_order)):
    """Look up many player UIDs. Poll the job (or set webhook_url) for the
    valid/invalid split."""
    await charge(identity, 1)  # player lookup: per request, not per UID
    rows = [{"player_id": pid} for pid in body.player_ids]
    return await sync_to_async(_create_job)(
        "player_info", None, body.country_code, rows, body.webhook_url or "",
        _owner_id(identity),
    )


@router.post("/code-status", response_model=BulkJobResponse,
             summary="Bulk code status check (valid/used/invalid)")
async def bulk_code_status(body: BulkCodeRequest, identity: dict = Depends(require_order)):
    """For one player, check many codes. Does NOT redeem."""
    await charge(identity, len(body.pin_codes))  # one upstream call per code
    rows = [{"player_id": body.player_id, "pin_code": c, "zone_id": body.zone_id}
            for c in body.pin_codes]
    return await sync_to_async(_create_job)(
        "validate", None, body.country_code, rows, body.webhook_url or "",
        _owner_id(identity),
    )


@router.post("/redeem", response_model=BulkJobResponse, summary="Bulk redeem")
async def bulk_redeem(body: BulkCodeRequest, identity: dict = Depends(require_order)):
    """For one player, redeem many codes (lookup -> validate -> redeem each)."""
    await charge(identity, len(body.pin_codes))  # one upstream redeem per code
    rows = [{"player_id": body.player_id, "pin_code": c, "zone_id": body.zone_id}
            for c in body.pin_codes]
    return await sync_to_async(_create_job)(
        "redeem", None, body.country_code, rows, body.webhook_url or "",
        _owner_id(identity),
    )


@router.get("/jobs/{job_id}", response_model=BulkJobDetailResponse,
            summary="Poll a job (status + valid/invalid + items)")
async def get_job(job_id: int, identity: dict = Depends(require_order)):
    detail = await sync_to_async(_get_job_detail)(identity, job_id)
    if detail is None:
        raise HTTPException(404, f"job {job_id} not found")
    return detail


@router.get("/jobs/{job_id}/items", response_model=list[BulkJobItemResponse],
            summary="Paginated items for a job")
async def get_job_items(
    job_id: int,
    identity: dict = Depends(require_order),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    items = await sync_to_async(_get_items)(identity, job_id, limit, offset)
    if items is None:
        raise HTTPException(404, f"job {job_id} not found")
    return items
