import logging
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "midasbuy_project.settings")
django.setup()

from asgiref.sync import sync_to_async
from fastapi import Depends, FastAPI, HTTPException, Query

from .auth_routes import router as auth_router
from .bulk_routes import router as bulk_router
from .dependencies import charge, require_order
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


async def _select_session(metered: bool) -> tuple[int, str, str]:
    """Server picks the bot account (clients don't choose). Returns
    (account_id, storage_state_path, cookie_header) or raises 503."""
    from accounts.services.rotation import pick_account

    acct, reason = await sync_to_async(pick_account)(metered)
    if acct is None:
        raise HTTPException(status_code=503, detail=reason or "No account available.")
    ssp, cookies = await sync_to_async(_resolve_session_sync)(acct.id)
    if not ssp:
        raise HTTPException(status_code=503,
                            detail="Selected account has no valid session. Log it in first.")
    return acct.id, ssp, cookies


def _result_ok(result) -> bool:
    """Extract `success` from a service result that may be a Pydantic model
    (player lookup / redeem) or a plain dict (code-status / all-in-one redeem)."""
    if isinstance(result, dict):
        return bool(result.get("success"))
    return bool(getattr(result, "success", False))


async def _report(account_id: int | None, result) -> None:
    from accounts.services.rotation import report_result

    await sync_to_async(report_result)(account_id, _result_ok(result))


@api_app.get("/player-info", response_model=PlayerLookupResponse)
async def player_info(
    player_id:    str = Query(...),
    country_code: str = Query("bd"),
    identity:     dict = Depends(require_order),
):
    await charge(identity, 1)  # player lookup: per request
    account_id, ssp, cookies = await _select_session(metered=False)
    result = await get_player_info(player_id, country_code, ssp, cookies)
    await _report(account_id, result)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)
    return result


@api_app.post("/code-status", response_model=CodeActionResponse, tags=["single"])
async def code_status(body: CodeActionRequest, identity: dict = Depends(require_order)):
    """Check one code for one player: valid / used / invalid. Does NOT redeem."""
    await charge(identity, 1)
    account_id, ssp, cookies = await _select_session(metered=True)
    result = await check_code_status(body.player_id, body.pin_code, body.country_code, ssp, cookies)
    await _report(account_id, result)
    return result


@api_app.post("/redeem-now", response_model=CodeActionResponse, tags=["single"])
async def redeem_now(body: CodeActionRequest, identity: dict = Depends(require_order)):
    """All-in-one: look up player -> validate code -> redeem, in a single call."""
    await charge(identity, 1)  # one redeem item
    account_id, ssp, cookies = await _select_session(metered=True)
    result = await redeem_all_in_one(body.player_id, body.pin_code, body.country_code, ssp, cookies)
    await _report(account_id, result)
    return result


@api_app.post("/redeem", response_model=RedeemResponse, tags=["single"])
async def redeem(body: RedeemRequest, identity: dict = Depends(require_order)):
    """Interactive two-step flow used by the panel (validate, then confirm)."""
    # Charge once per redeem item — on the initiating call only, so the follow-up
    # confirm and verification retries for the same item don't double-count.
    if not body.confirm:
        await charge(identity, 1)

    # First call: the server picks the account and returns its id; the panel echoes
    # that id back on confirm/verification so the whole flow stays on one session.
    if body.account_id:
        account_id = body.account_id
        ssp, cookies = await _resolve_session(account_id)
        if not ssp:
            raise HTTPException(status_code=503, detail="Account session expired. Retry.")
    else:
        account_id, ssp, cookies = await _select_session(metered=True)

    result = await submit_redeem(
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
    result.account_id = account_id
    # Only judge account health on terminal outcomes (not mid-flow prompts).
    if not (result.verification_required or result.confirmation_required):
        await _report(account_id, result)
    return result
