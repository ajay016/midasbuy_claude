"""
Crypto primitives for API auth: JWT (browser/dashboard) and HMAC request signing
(server-to-server). Pure functions — no DB access — so they're easy to test.

HMAC signing scheme (what a client must reproduce)
--------------------------------------------------
Canonical string, fields joined by '\n' in this exact order:

    METHOD            e.g. POST
    PATH              request path, e.g. /api/bulk/redeem
    QUERY             raw query string ('' if none)
    BODY_SHA256       hex sha256 of the raw request body ('' body -> sha256 of b'')
    TIMESTAMP         unix seconds, as sent in X-Timestamp
    NONCE             random unique string, as sent in X-Nonce

    signature = hex( HMAC_SHA256(secret, canonical_string) )

Headers the client sends: X-Api-Key, X-Timestamp, X-Nonce, X-Signature.
The server rejects if the timestamp is outside +/- SIGNATURE_WINDOW or the nonce
was already used (replay).
"""
import base64
import hashlib
import hmac
import time
from functools import lru_cache
from typing import Optional

import jwt
from cryptography.fernet import Fernet
from django.conf import settings

# ── Tunables ───────────────────────────────────────────────────────────────────
ACCESS_TTL = 15 * 60            # seconds
REFRESH_TTL = 7 * 24 * 60 * 60  # seconds
SIGNATURE_WINDOW = 5 * 60       # seconds of allowed clock skew
ALGORITHM = "HS256"


def _jwt_secret() -> str:
    return getattr(settings, "JWT_SECRET", None) or settings.SECRET_KEY


# ── Secret encryption at rest (Fernet) ─────────────────────────────────────────
# API secrets must be recoverable to verify HMAC signatures, so we store them
# ENCRYPTED rather than hashed. The encryption key lives in env/settings
# (APIAUTH_FERNET_KEY), separate from the database — a DB-only leak is useless
# without it. In dev we derive a stable key from SECRET_KEY as a fallback.
@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = getattr(settings, "APIAUTH_FERNET_KEY", None)
    if not key:
        digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest).decode("ascii")
    if isinstance(key, str):
        key = key.encode("ascii")
    return Fernet(key)


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


# ── JWT ────────────────────────────────────────────────────────────────────────
def _make_token(merchant_id: int, token_type: str, ttl: int) -> str:
    now = int(time.time())
    payload = {
        "sub": str(merchant_id),
        "type": token_type,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=ALGORITHM)


def make_access_token(merchant_id: int) -> str:
    return _make_token(merchant_id, "access", ACCESS_TTL)


def make_refresh_token(merchant_id: int) -> str:
    return _make_token(merchant_id, "refresh", REFRESH_TTL)


def decode_token(token: str, expected_type: str = "access") -> Optional[int]:
    """Return merchant_id if the token is valid and of the expected type, else None."""
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != expected_type:
        return None
    try:
        return int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        return None


# ── HMAC request signing ───────────────────────────────────────────────────────
def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def canonical_string(method: str, path: str, query: str, body_hash: str,
                     timestamp: str, nonce: str) -> str:
    return "\n".join([method.upper(), path, query or "", body_hash, timestamp, nonce])


def sign(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_signature(secret: str, canonical: str, provided_signature: str) -> bool:
    expected = sign(secret, canonical)
    return hmac.compare_digest(expected, provided_signature or "")


def webhook_signature(body: bytes) -> str:
    """HMAC-SHA256 over the webhook body so the receiver can verify it's from us.
    Receiver verifies: hex(HMAC_SHA256(WEBHOOK_SECRET, raw_body)) == X-Webhook-Signature."""
    secret = getattr(settings, "WEBHOOK_SECRET", None) or _jwt_secret()
    return hmac.new(secret.encode("utf-8"), body or b"", hashlib.sha256).hexdigest()


def timestamp_fresh(timestamp: str, now: Optional[int] = None) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = now if now is not None else int(time.time())
    return abs(now - ts) <= SIGNATURE_WINDOW
