import logging
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "midasbuy_project.settings")
django.setup()

from asgiref.sync import sync_to_async
from fastapi import FastAPI, HTTPException, Query

from .schemas import PlayerLookupResponse, RedeemRequest, RedeemResponse
from .service import get_player_info, submit_redeem

logger = logging.getLogger(__name__)
api_app = FastAPI(title="Midasbuy Redeem API", version="2.0.0")


@api_app.on_event("startup")
async def prewarm_browser_sessions():
    """
    Kick off the browser cold-start in the background as soon as the server
    boots, so the FIRST real player/redeem request reuses a ready session
    instead of paying the launch + navigate + xMidas warm-up cost inline.

    Best-effort and non-blocking: never delays startup, never crashes it.
    Disable with MIDASBUY_PREWARM_SESSIONS=0.
    """
    import asyncio

    if os.getenv("MIDASBUY_PREWARM_SESSIONS", "1") not in ("1", "true", "True"):
        logger.info("[API] session pre-warm disabled")
        return

    asyncio.create_task(_run_prewarm())


def _active_sessions_sync() -> list[tuple[str, str]]:
    """Return (storage_state_path, country) for accounts with a session on disk."""
    from accounts.models import MidasbuyAccount
    from django.conf import settings

    country = os.getenv("MIDASBUY_PREWARM_COUNTRY", "bd")
    max_sessions = int(os.getenv("MIDASBUY_PREWARM_MAX", "2"))

    out: list[tuple[str, str]] = []
    qs = MidasbuyAccount.objects.all().order_by("-last_login_at", "-updated_at")
    for acct in qs:
        ssp = acct.storage_state_path or os.path.join(
            acct.get_session_dir(str(settings.BASE_DIR)), "storage_state.json"
        )
        if ssp and os.path.exists(ssp):
            out.append((ssp, country))
        if len(out) >= max_sessions:
            break
    return out


async def _run_prewarm():
    from accounts.services.playwright_crypto import warm_cached_session

    from .service import _run_browser_call

    try:
        sessions = await sync_to_async(_active_sessions_sync, thread_sensitive=True)()
    except Exception:
        logger.exception("[API] pre-warm: could not resolve active sessions")
        return

    if not sessions:
        logger.info("[API] pre-warm: no stored sessions to warm")
        return

    for ssp, country in sessions:
        try:
            logger.info("[API] pre-warming browser session ssp=%s country=%s", ssp, country)
            await _run_browser_call(warm_cached_session, ssp, country)
        except Exception:
            logger.exception("[API] pre-warm failed for %s", ssp)


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
):
    ssp, cookies = await _resolve_session(account_id)
    result = await get_player_info(player_id, country_code, ssp, cookies)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)
    return result


@api_app.post("/redeem", response_model=RedeemResponse)
async def redeem(body: RedeemRequest):
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
