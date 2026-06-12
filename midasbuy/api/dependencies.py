"""
The single auth gate for every /api endpoint.

A request authenticates with EITHER:
  * Authorization: Bearer <jwt>            (dashboard / browser users), or
  * X-Api-Key + X-Timestamp + X-Nonce + X-Signature   (server-to-server HMAC).

Both resolve to the same Merchant. Use it on a route with:

    @api_app.get("/whatever")
    async def handler(merchant: dict = Depends(require_auth)):
        ...
"""
import logging

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
    from apiauth.models import Merchant
    from apiauth.security import decode_token

    merchant_id = decode_token(token, expected_type="access")
    if not merchant_id:
        return None
    m = Merchant.objects.filter(pk=merchant_id, is_active=True).first()
    if not m:
        return None
    return {"merchant_id": m.id, "name": m.name, "auth": "jwt", **m.capabilities()}


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

    key = ApiKey.objects.select_related("merchant").filter(
        key_id=key_id, is_active=True
    ).first()
    if not key or not key.merchant.is_active:
        return None

    # Block replays BEFORE doing the (cheap) signature check.
    if not _nonce_unused(key_id, nonce, SIGNATURE_WINDOW * 2):
        return None

    canonical = canonical_string(method, path, query, body_sha256(body), timestamp, nonce)
    if not verify_signature(key.get_secret(), canonical, signature):
        return None

    return {"merchant_id": key.merchant_id, "name": key.merchant.name, "auth": "hmac",
            "key_id": key_id, **key.merchant.capabilities()}


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
    async def _dep(merchant: dict = Depends(require_auth)) -> dict:
        if not merchant.get(flag):
            raise HTTPException(status_code=403, detail=detail)
        return merchant

    return _dep


require_order = require_capability(
    "can_order", "Your account isn't permitted to place orders."
)
require_api_access = require_capability(
    "can_use_api", "API access isn't enabled on your account."
)
