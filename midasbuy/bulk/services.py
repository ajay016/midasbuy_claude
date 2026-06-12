"""
Sync bridge between Celery tasks and the existing async redeem service.

The redeem logic in api/service.py is async (it drives the browser via the
event loop). Celery workers are plain sync functions, so each item runs the
coroutine with asyncio.run(). The browser session and its executors live at
module scope inside the worker process, so they warm up once and are reused
across every item in the worker — the same speed-up the web server gets.
"""
import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger("bulk")


# ── Session resolution (same logic the FastAPI app uses) ───────────────────────
def resolve_session(account_id: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """Return (storage_state_path, cookie_header) for a logged-in account."""
    from django.conf import settings

    from accounts.models import MidasbuyAccount

    if account_id is None:
        return None, None

    try:
        acct = MidasbuyAccount.objects.get(pk=account_id)
    except MidasbuyAccount.DoesNotExist:
        logger.warning("[BULK] account_id=%s not found", account_id)
        return None, None

    ssp = acct.storage_state_path or os.path.join(
        acct.get_session_dir(str(settings.BASE_DIR)), "storage_state.json"
    )
    if not ssp or not os.path.exists(ssp):
        logger.warning("[BULK] storage_state missing for account_id=%s ssp=%s", account_id, ssp)
        return None, None

    cookie_header = ""
    if acct.cookie_data:
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}"
            for c in acct.cookie_data
            if c.get("name") and c.get("value")
        )
    else:
        cookie_header = acct.get_cookie_header()

    return ssp, cookie_header


def _run(coro):
    """Run one coroutine to completion in a throwaway event loop."""
    return asyncio.run(coro)


# ── Per-item processors ────────────────────────────────────────────────────────
# Each returns a normalised dict: {success, message, username?, product_name?, raw}

def process_player_info(player_id: str, country_code: str, ssp: str, cookies: str) -> dict:
    from api.service import get_player_info

    res = _run(get_player_info(player_id, country_code, ssp, cookies))
    if res.success and res.player:
        return {
            "success": True,
            "message": "Player found.",
            "username": res.player.username,
            "zone_id": res.player.zone_id or "",
            "raw": res.player.model_dump(),
        }
    return {"success": False, "message": res.error or "Player not found.", "raw": {}}


def process_validate(player_id: str, pin_code: str, country_code: str,
                     ssp: str, cookies: str, zone_id: str = "1") -> dict:
    """Look the player up, then check whether the code is valid (no redeem)."""
    from api.service import get_player_info, submit_redeem

    info = _run(get_player_info(player_id, country_code, ssp, cookies))
    if not info.success or not info.player:
        return {"success": False, "message": info.error or "Player not found.", "raw": {}}

    zid = info.player.zone_id or zone_id or "1"
    check = _run(submit_redeem(
        player_id, pin_code, country_code, ssp, cookies, zid, confirm=False,
    ))
    # submit_redeem(confirm=False) returns confirmation_required=True for a valid code.
    if check.confirmation_required:
        return {
            "success": True,
            "message": check.message or "Code is valid.",
            "username": info.player.username,
            "product_name": check.product_name or "",
            "zone_id": zid,
            "raw": check.raw or {},
        }
    return {
        "success": False,
        "message": check.message or "Code is not valid.",
        "username": info.player.username,
        "raw": check.raw or {},
    }


def process_redeem(player_id: str, pin_code: str, country_code: str,
                   ssp: str, cookies: str, zone_id: str = "1") -> dict:
    """Full flow: look up player -> validate code -> redeem."""
    from api.service import get_player_info, submit_redeem

    info = _run(get_player_info(player_id, country_code, ssp, cookies))
    if not info.success or not info.player:
        return {"success": False, "message": info.error or "Player not found.", "raw": {}}

    zid = info.player.zone_id or zone_id or "1"
    username = info.player.username

    # Step 1: validate (also yields product_name / product_id for the commit).
    check = _run(submit_redeem(
        player_id, pin_code, country_code, ssp, cookies, zid, confirm=False,
    ))
    if not check.confirmation_required:
        return {
            "success": False,
            "message": check.message or "Code is not valid; nothing redeemed.",
            "username": username,
            "raw": check.raw or {},
        }

    # Step 2: commit the redemption.
    done = _run(submit_redeem(
        player_id, pin_code, country_code, ssp, cookies, zid,
        confirm=True,
        product_name=check.product_name,
        product_id=check.product_id,
    ))
    return {
        "success": bool(done.success),
        "message": done.message,
        "username": username,
        "product_name": check.product_name or "",
        "zone_id": zid,
        "raw": done.raw or {},
    }
