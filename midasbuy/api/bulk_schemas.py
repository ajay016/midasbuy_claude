"""Pydantic schemas for the bulk endpoints."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Requests ───────────────────────────────────────────────────────────────────
class BulkPlayerInfoRequest(BaseModel):
    account_id: int = Field(..., description="Logged-in MidasbuyAccount to use")
    country_code: str = Field("bd", description="ISO country / storefront code")
    player_ids: list[str] = Field(..., min_length=1, description="PUBG Mobile UIDs to look up")
    webhook_url: Optional[str] = Field(
        None, description="If set, the finished job result is POSTed here (signed)."
    )


class BulkCodeRequest(BaseModel):
    """One player + many codes (matches the bulk plan; extends later to many players)."""
    account_id: int = Field(..., description="Logged-in MidasbuyAccount to use")
    country_code: str = Field("bd", description="ISO country / storefront code")
    player_id: str = Field(..., description="PUBG Mobile UID that owns these codes")
    pin_codes: list[str] = Field(..., min_length=1, description="UC redeem pin codes")
    zone_id: str = Field("1", description="Zone ID (filled from lookup if omitted)")
    webhook_url: Optional[str] = Field(
        None, description="If set, the finished job result is POSTed here (signed)."
    )


# ── Responses ──────────────────────────────────────────────────────────────────
class BulkJobResponse(BaseModel):
    job_id: int
    job_type: str
    status: str
    account_id: Optional[int]
    country_code: str
    total_items: int
    processed_items: int
    succeeded_items: int
    failed_items: int
    progress_percent: int
    webhook_url: str = ""
    created_at: datetime


class BulkJobItemResponse(BaseModel):
    id: int
    player_id: str
    pin_code: str
    zone_id: str
    status: str
    success: bool
    message: str
    username: str
    product_name: str


class BulkJobDetailResponse(BulkJobResponse):
    # Convenience split requested for the API: succeeded vs failed rows.
    valid: list[BulkJobItemResponse] = []
    invalid: list[BulkJobItemResponse] = []
    items: list[BulkJobItemResponse] = []
