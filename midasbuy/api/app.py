import logging
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "midasbuy_project.settings")
django.setup()

from asgiref.sync import sync_to_async
from fastapi import Depends, FastAPI, HTTPException, Query

from .auth_routes import router as auth_router
from .bulk_routes import router as bulk_router
from .dependencies import require_order
from .schemas import (
    CodeActionRequest,
    CodeActionResponse,
    PlayerLookupResponse,
    RedeemRequest,
    RedeemResponse,
)
from .service import check_code_status, get_player_info, redeem_all_in_one, submit_redeem

logger = logging.getLogger(__name__)
api_app = FastAPI(title="Midasbuy Redeem API", version="2.0.0")

# Auth (register/login/refresh/api-keys) under /api/auth/* — public + protected.
api_app.include_router(auth_router)
# Bulk operations under /api/bulk/* — every route requires auth (set on the router).
api_app.include_router(bulk_router)


@api_app.on_event("shutdown")
async def shutdown_browser_cache():
    from .service import shutdown_browser_worker
    await shutdown_browser_worker()


def _resolve_session_sync(account_id: int) -> tuple[str | None, str | None]:
    """Sync helper — must be called via sync_to_async from async endpoints."""
    from accounts.models import MidasbuyAccount
    from django.conf import settings

    try:
        acct = MidasbuyAccount.objects.get(pk=account_id)
    except MidasbuyAccount.DoesNotExist:
        logger.warning("[API] account_id=%s not found", account_id)
        return None, None

    # Prefer the path stored by the login service; fall back to computed path
    ssp = acct.storage_state_path or os.path.join(
        acct.get_session_dir(str(settings.BASE_DIR)), "storage_state.json"
    )

    if not ssp or not os.path.exists(ssp):
        logger.warning(
            "[API] storage_state not found for account_id=%s ssp=%s", account_id, ssp
        )
        return None, None

    # Build cookie header — prefer in-DB cookie_data JSON, fall back to file
    cookie_header = ""
    if acct.cookie_data:
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}"
            for c in acct.cookie_data
            if c.get("name") and c.get("value")
        )
    else:
        cookie_header = acct.get_cookie_header()

    logger.info(
        "[API] resolved session account_id=%s ssp=%s cookie_count=%s",
        account_id,
        ssp,
        len(acct.cookie_data) if acct.cookie_data else 0,
    )
    return ssp, cookie_header


async def _resolve_session(account_id: int | None) -> tuple[str | None, str | None]:
    if account_id is None:
        logger.warning("[API] no account_id provided")
        return None, None
    return await sync_to_async(_resolve_session_sync)(account_id)


@api_app.get("/player-info", response_model=PlayerLookupResponse)
async def player_info(
    player_id:    str = Query(...),
    country_code: str = Query("bd"),
    account_id:   int | None = Query(None),
    identity:     dict = Depends(require_order),
):
    ssp, cookies = await _resolve_session(account_id)
    result = await get_player_info(player_id, country_code, ssp, cookies)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)
    return result


@api_app.post("/code-status", response_model=CodeActionResponse, tags=["single"])
async def code_status(body: CodeActionRequest, identity: dict = Depends(require_order)):
    """Check one code for one player: valid / used / invalid. Does NOT redeem."""
    ssp, cookies = await _resolve_session(body.account_id)
    return await check_code_status(body.player_id, body.pin_code, body.country_code, ssp, cookies)


@api_app.post("/redeem-now", response_model=CodeActionResponse, tags=["single"])
async def redeem_now(body: CodeActionRequest, identity: dict = Depends(require_order)):
    """All-in-one: look up player -> validate code -> redeem, in a single call."""
    ssp, cookies = await _resolve_session(body.account_id)
    return await redeem_all_in_one(body.player_id, body.pin_code, body.country_code, ssp, cookies)


@api_app.post("/redeem", response_model=RedeemResponse, tags=["single"])
async def redeem(body: RedeemRequest, identity: dict = Depends(require_order)):
    """Interactive two-step flow used by the panel (validate, then confirm)."""
    ssp, cookies = await _resolve_session(body.account_id)
    return await submit_redeem(
        body.player_id,
        body.pin_code,
        body.country_code,
        ssp,
        cookies,
        body.zone_id,
        body.rc_token,
        body.rc_uuid,
        body.confirm,
        body.product_name,
        body.product_id,
    )
