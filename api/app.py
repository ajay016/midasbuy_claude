import logging
import os
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "midasbuy_project.settings")
django.setup()

from asgiref.sync import sync_to_async
from fastapi import Depends, FastAPI, HTTPException, Query, Request

from .auth_routes import router as auth_router
from .bulk_routes import router as bulk_router
from .partner_routes import router as partner_router
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


# ── Request timing ─────────────────────────────────────────────────────────────
# Stamp every request at the very start (before auth runs) so handlers can measure
# each phase. Total wall time is returned on the `Server-Timing` response header
# (visible in any browser's Network tab) and `X-Process-Time-Ms`.
@api_app.middleware("http")
async def _timing_middleware(request: Request, call_next):
    request.state.t_start = time.perf_counter()
    request.state.spans = {}
    response = await call_next(request)
    total_ms = round((time.perf_counter() - request.state.t_start) * 1000, 1)
    parts = [f"total;dur={total_ms}"] + [
        f"{k};dur={v}" for k, v in getattr(request.state, "spans", {}).items()
    ]
    response.headers["Server-Timing"] = ", ".join(parts)
    response.headers["X-Process-Time-Ms"] = str(total_ms)
    return response


class _Timer:
    """Measures per-phase server time. `auth` = everything before the handler ran
    (signature verify, nonce/rate-limit Redis, DB lookups)."""
    def __init__(self, request: Request):
        self.request = request
        t_start = getattr(request.state, "t_start", time.perf_counter())
        self.prev = time.perf_counter()
        self.spans = {"auth": round((self.prev - t_start) * 1000, 1)}
        self.t_start = t_start

    def mark(self, name: str):
        now = time.perf_counter()
        self.spans[name] = round((now - self.prev) * 1000, 1)
        self.prev = now

    def finish(self) -> dict:
        self.spans["server_total"] = round((time.perf_counter() - self.t_start) * 1000, 1)
        self.request.state.spans = dict(self.spans)
        return self.spans


# Auth (register/login/refresh/api-keys) under /api/auth/* — public + protected.
api_app.include_router(auth_router)
# Bulk operations under /api/bulk/* — every route requires auth (set on the router).
api_app.include_router(bulk_router)
# Partner self-service under /api/partner/* — every route requires a partner/admin.
api_app.include_router(partner_router)


@api_app.on_event("shutdown")
async def shutdown_browser_cache():
    global _prewarm_task
    if _prewarm_task is not None:
        _prewarm_task.cancel()
        _prewarm_task = None
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

    t0 = time.perf_counter()
    acct, reason = await sync_to_async(pick_account)(metered)
    t1 = time.perf_counter()
    if acct is None:
        raise HTTPException(status_code=503, detail=reason or "No account available.")
    ssp, cookies = await sync_to_async(_resolve_session_sync)(acct.id)
    t2 = time.perf_counter()
    # Splits the 'session' timing into account-pick vs session-resolve so a slow
    # step (e.g. a blocking Redis or DB call) is obvious in the logs.
    logger.info("[TIMING] pick_account=%.0fms resolve_session=%.0fms",
                (t1 - t0) * 1000, (t2 - t1) * 1000)
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


# ── Browser warm-up ────────────────────────────────────────────────────────────
# Each logged-in account has its own cached browser session (per country). The
# first call for an account is a slow cold start (launch Chromium, load the SDK);
# after that it's reused. We pre-warm every account on startup — and refresh
# periodically — so rotation between accounts stays fast, not just the first call.
def _logged_in_account_sessions() -> list[tuple[int, str]]:
    """(account_id, storage_state_path) for every logged-in account with a session."""
    from accounts.models import MidasbuyAccount

    out = []
    for acct in MidasbuyAccount.objects.filter(status=1):
        ssp, _ = _resolve_session_sync(acct.id)
        if ssp:
            out.append((acct.id, ssp))
    return out


async def _warm_all_sessions() -> None:
    from django.conf import settings

    from .service import warm_account_session

    countries = [c.strip().lower()
                 for c in getattr(settings, "MIDASBUY_WARM_COUNTRIES", ["bd"]) if c.strip()]
    sessions = await sync_to_async(_logged_in_account_sessions)()
    if not sessions:
        logger.info("[WARMUP] no logged-in accounts to warm yet")
        return
    logger.info("[WARMUP] warming %d account(s) x %d country(ies)",
                len(sessions), len(countries))
    # Sequential awaits keep the single browser thread interleaved with live traffic
    # (a real request waits at most one warm-up, not the whole sweep).
    for account_id, ssp in sessions:
        for country in countries:
            ok = await warm_account_session(ssp, country)
            logger.info("[WARMUP] account=%s country=%s warm=%s", account_id, country, ok)


async def _prewarm_loop() -> None:
    import asyncio

    from django.conf import settings

    try:
        await _warm_all_sessions()
    except Exception:
        logger.exception("[WARMUP] initial warm-up failed")

    interval = int(getattr(settings, "MIDASBUY_WARM_INTERVAL", 0) or 0)
    while interval > 0:
        try:
            await asyncio.sleep(interval)
            await _warm_all_sessions()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[WARMUP] periodic warm-up sweep failed")


_prewarm_task = None


@api_app.on_event("startup")
async def _start_prewarm():
    import asyncio

    from django.conf import settings

    if not getattr(settings, "MIDASBUY_WARM_ON_STARTUP", True):
        return
    global _prewarm_task
    _prewarm_task = asyncio.create_task(_prewarm_loop())
    logger.info("[WARMUP] background pre-warm scheduled")


@api_app.get("/player-info", response_model=PlayerLookupResponse)
async def player_info(
    request:      Request,
    player_id:    str = Query(...),
    country_code: str = Query("bd"),
    identity:     dict = Depends(require_order),
):
    t = _Timer(request)
    await charge(identity, 1)  # player lookup: per request
    t.mark("charge")
    account_id, ssp, cookies = await _select_session(metered=False)
    t.mark("session")
    result = await get_player_info(player_id, country_code, ssp, cookies)
    t.mark("upstream")
    await _report(account_id, result)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)
    result.timings = t.finish()
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
