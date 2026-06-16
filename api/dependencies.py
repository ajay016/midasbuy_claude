"""
The single auth gate for every /api endpoint.

A request authenticates with EITHER:
  * Authorization: Bearer <jwt>            (dashboard / browser users), or
  * X-Api-Key + X-Timestamp + X-Nonce + X-Signature   (server-to-server HMAC).

Both resolve to the same User. Use it on a route with:

    @api_app.get("/whatever")
    async def handler(identity: dict = Depends(require_auth)):
        ...
"""
import logging
import time

from asgiref.sync import sync_to_async
from fastapi import Depends, HTTPException, Request

logger = logging.getLogger("api")


# ── Sync verification (Django ORM + Redis + crypto) ────────────────────────────
def _nonce_unused(key_id: str, nonce: str, window: int) -> bool:
    """True the first time a (key, nonce) pair is seen; False on replay."""
    from django.conf import settings

    try:
        import redis

        client = redis.from_url(settings.CELERY_BROKER_URL)
        # SET NX returns True only if the key did not exist -> first use.
        return bool(client.set(f"apiauth:nonce:{key_id}:{nonce}", "1", nx=True, ex=window))
    except Exception:
        # If Redis is unreachable, fail CLOSED (reject) — never weaken auth.
        logger.exception("[AUTH] nonce store unavailable; rejecting request")
        return False


def _auth_jwt(token: str):
    from django.contrib.auth import get_user_model

    from apiauth.security import decode_token

    user_id = decode_token(token, expected_type="access")
    if not user_id:
        return None
    User = get_user_model()
    u = User.objects.filter(pk=user_id, is_active=True).first()
    if not u:
        return None
    return {"user_id": u.id, "name": u.name, "auth": "jwt", **u.capabilities()}


def _auth_hmac(headers: dict, method: str, path: str, query: str, body: bytes):
    from apiauth.models import ApiKey
    from apiauth.security import (
        SIGNATURE_WINDOW,
        body_sha256,
        canonical_string,
        timestamp_fresh,
        verify_signature,
    )

    key_id = headers.get("x-api-key")
    timestamp = headers.get("x-timestamp")
    nonce = headers.get("x-nonce")
    signature = headers.get("x-signature")
    if not (key_id and timestamp and nonce and signature):
        return None

    if not timestamp_fresh(timestamp):
        return None

    key = ApiKey.objects.select_related("user").filter(
        key_id=key_id, is_active=True
    ).first()
    if not key or not key.user.is_active:
        return None

    # Block replays BEFORE doing the (cheap) signature check.
    if not _nonce_unused(key_id, nonce, SIGNATURE_WINDOW * 2):
        return None

    canonical = canonical_string(method, path, query, body_sha256(body), timestamp, nonce)
    if not verify_signature(key.get_secret(), canonical, signature):
        return None

    return {"user_id": key.user_id, "name": key.user.name, "auth": "hmac",
            "key_id": key_id, **key.user.capabilities()}


def _authenticate(method: str, path: str, query: str, body: bytes, headers: dict):
    auth_header = headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return _auth_jwt(auth_header[len("Bearer "):].strip())
    if headers.get("x-api-key"):
        return _auth_hmac(headers, method, path, query, body)
    return None


# ── FastAPI dependency ─────────────────────────────────────────────────────────
async def require_auth(request: Request) -> dict:
    # Read the raw body once; FastAPI caches it so route body-parsing still works.
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    info = await sync_to_async(_authenticate)(
        request.method, request.url.path, request.url.query, body, headers
    )
    if not info:
        raise HTTPException(
            status_code=401,
            detail="Authentication required (valid JWT or signed API request).",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return info


def require_capability(flag: str, detail: str):
    """Build a dependency that authenticates AND requires a capability flag.

    Authentication (401) is handled by require_auth; this adds the authorization
    (403) check on top, so an authenticated user who lacks the capability is told
    so explicitly rather than getting a vague 401.
    """
    async def _dep(identity: dict = Depends(require_auth)) -> dict:
        if not identity.get(flag):
            raise HTTPException(status_code=403, detail=detail)
        return identity

    return _dep


require_api_access = require_capability(
    "can_use_api", "API access isn't enabled on your account."
)


# ── Rate limiting (per user, per minute) ───────────────────────────────────────
def _rate_ok(user_id: int, limit: int) -> bool:
    """Fixed-window per-minute limiter in Redis. 0 = unlimited.

    Fails OPEN (allows) if Redis is unreachable — unlike replay protection, a
    missing rate limiter shouldn't take the whole API down."""
    if not limit or limit <= 0:
        return True
    from django.conf import settings

    try:
        import redis

        client = redis.from_url(settings.CELERY_BROKER_URL)
        window = int(time.time() // 60)
        key = f"ratelimit:{user_id}:{window}"
        n = client.incr(key)
        if n == 1:
            client.expire(key, 90)
        return n <= limit
    except Exception:
        logger.warning("[RATE] limiter unavailable; allowing request")
        return True


def plan_for(identity: dict) -> str:
    """Which meter a request draws down: dashboard (JWT) -> panel, API (HMAC) -> api."""
    return "panel" if identity.get("auth") == "jwt" else "api"


# ── Quota metering ─────────────────────────────────────────────────────────────
def _charge_sync(user_id: int, role: str, plan: str, n: int) -> str | None:
    """Charge ``n`` units against the client's plan. Returns an error string to
    raise as 402, or None on success. Admins/staff are internal -> never metered."""
    from apiauth.models import User

    if role != User.ROLE_CLIENT:
        return None  # unlimited for admins and staff

    from billing.models import Subscription

    sub = Subscription.current_for(user_id, plan)
    if sub is None:
        return f"No active {plan} subscription. Ask an admin to enable it."
    if not sub.consume(n):
        return (f"Your {plan} quota ({sub.request_limit} requests / 30 days) can't cover "
                f"this ({n} request{'s' if n != 1 else ''}). It resets on "
                f"{sub.period_end:%Y-%m-%d}.")
    return None


async def require_order(identity: dict = Depends(require_auth)) -> dict:
    """Authenticated + permitted to order + within the per-minute rate limit.

    NOTE: this does NOT charge quota — call ``charge()`` from the endpoint so the
    cost can vary (per-request for lookups, per-item for bulk redeem)."""
    if not identity.get("can_order"):
        raise HTTPException(status_code=403,
                            detail="Your account isn't permitted to place orders.")
    if not identity.get("is_admin"):  # admins are never rate limited
        ok = await sync_to_async(_rate_ok)(
            identity["user_id"], identity.get("rate_limit_per_min", 20)
        )
        if not ok:
            raise HTTPException(status_code=429,
                                detail="Rate limit exceeded. Slow down and retry shortly.")
    return identity


async def charge(identity: dict, n: int = 1) -> None:
    """Charge ``n`` request-units against the caller's meter (402 if it can't cover).

    The plan depends on how they authenticated (JWT -> panel, HMAC -> api), so the
    two meters are independent. Cost per call:
      * player lookups (single + bulk)  -> 1 (per request)
      * single redeem / code-status     -> 1
      * bulk redeem / bulk code-status  -> one per item (matches upstream calls)
    """
    err = await sync_to_async(_charge_sync)(
        identity["user_id"], identity.get("role"), plan_for(identity), n
    )
    if err:
        raise HTTPException(status_code=402, detail=err)
