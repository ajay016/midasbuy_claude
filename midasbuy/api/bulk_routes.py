"""
Bulk operation endpoints (mounted under /api/bulk).

These endpoints are *non-blocking*: they create a job + its rows in the database,
hand the job id to Celery, and return immediately. The browser work happens in a
worker. Clients poll GET /api/bulk/jobs/{id} for progress and results.
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
from .dependencies import require_auth

logger = logging.getLogger("api")
# Every /bulk/* route requires authentication (JWT or signed request).
router = APIRouter(prefix="/bulk", tags=["bulk"], dependencies=[Depends(require_auth)])


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


def _create_job(job_type: str, account_id: int, country_code: str, rows: list[dict]) -> dict:
    """Create the job + items and enqueue the Celery task. Returns the job dict."""
    from bulk.models import BulkJob, BulkJobItem
    from bulk.tasks import run_bulk_job

    job = BulkJob.objects.create(
        job_type=job_type,
        account_id=account_id,
        country_code=country_code,
        total_items=len(rows),
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


def _get_job(job_id: int):
    from bulk.models import BulkJob

    try:
        return _job_to_dict(BulkJob.objects.get(pk=job_id))
    except BulkJob.DoesNotExist:
        return None


def _get_items(job_id: int, limit: int, offset: int):
    from bulk.models import BulkJob, BulkJobItem

    if not BulkJob.objects.filter(pk=job_id).exists():
        return None
    qs = BulkJobItem.objects.filter(job_id=job_id).order_by("id")[offset:offset + limit]
    return [_item_to_dict(i) for i in qs]


# ── Endpoints ──────────────────────────────────────────────────────────────────
@router.post("/player-info", response_model=BulkJobResponse)
async def bulk_player_info(body: BulkPlayerInfoRequest):
    if not await sync_to_async(_account_ok)(body.account_id):
        raise HTTPException(404, f"account_id {body.account_id} not found")
    rows = [{"player_id": pid} for pid in body.player_ids]
    return await sync_to_async(_create_job)("player_info", body.account_id, body.country_code, rows)


@router.post("/validate", response_model=BulkJobResponse)
async def bulk_validate(body: BulkCodeRequest):
    if not await sync_to_async(_account_ok)(body.account_id):
        raise HTTPException(404, f"account_id {body.account_id} not found")
    rows = [i.model_dump() for i in body.items]
    return await sync_to_async(_create_job)("validate", body.account_id, body.country_code, rows)


@router.post("/redeem", response_model=BulkJobResponse)
async def bulk_redeem(body: BulkCodeRequest):
    if not await sync_to_async(_account_ok)(body.account_id):
        raise HTTPException(404, f"account_id {body.account_id} not found")
    rows = [i.model_dump() for i in body.items]
    return await sync_to_async(_create_job)("redeem", body.account_id, body.country_code, rows)


@router.get("/jobs/{job_id}", response_model=BulkJobDetailResponse)
async def get_job(job_id: int, include_items: bool = Query(True)):
    job = await sync_to_async(_get_job)(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    items = []
    if include_items:
        items = await sync_to_async(_get_items)(job_id, 1000, 0) or []
    return {**job, "items": items}


@router.get("/jobs/{job_id}/items", response_model=list[BulkJobItemResponse])
async def get_job_items(
    job_id: int,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    items = await sync_to_async(_get_items)(job_id, limit, offset)
    if items is None:
        raise HTTPException(404, f"job {job_id} not found")
    return items
