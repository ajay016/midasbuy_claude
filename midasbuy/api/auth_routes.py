"""
Authentication endpoints, mounted under /api/auth.

  POST /auth/register     create a merchant (and return tokens)
  POST /auth/login        email + password  -> access + refresh JWT
  POST /auth/refresh      refresh token     -> new access JWT
  POST /auth/api-keys     (auth) create a server-to-server API key (secret shown ONCE)
  GET  /auth/api-keys     (auth) list this merchant's keys (no secrets)
  DELETE /auth/api-keys/{key_id}  (auth) revoke a key
"""
from datetime import datetime

from asgiref.sync import sync_to_async
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from .dependencies import require_api_access, require_auth

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Schemas ────────────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1)
    email: EmailStr
    password: str = Field(..., min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"


class ApiKeyCreateRequest(BaseModel):
    label: str = Field("", max_length=120)


class ApiKeyCreatedResponse(BaseModel):
    key_id: str
    secret: str  # shown exactly once
    label: str


class ApiKeyItem(BaseModel):
    key_id: str
    label: str
    is_active: bool
    created_at: datetime


# ── Sync DB helpers ────────────────────────────────────────────────────────────
def _register(name: str, email: str, password: str) -> dict:
    from django.contrib.auth import get_user_model

    from apiauth.security import make_access_token, make_refresh_token

    User = get_user_model()
    if User.objects.filter(email__iexact=email).exists():
        return {"error": "email already registered"}
    u = User.objects.create_user(email=email, password=password, name=name)
    return {
        "access_token": make_access_token(u.id),
        "refresh_token": make_refresh_token(u.id),
    }


def _login(email: str, password: str) -> dict | None:
    from django.contrib.auth import get_user_model

    from apiauth.security import make_access_token, make_refresh_token

    User = get_user_model()
    u = User.objects.filter(email__iexact=email, is_active=True).first()
    if not u or not u.check_password(password):
        return None
    return {
        "access_token": make_access_token(u.id),
        "refresh_token": make_refresh_token(u.id),
    }


def _refresh(refresh_token: str) -> dict | None:
    from django.contrib.auth import get_user_model

    from apiauth.security import decode_token, make_access_token

    user_id = decode_token(refresh_token, expected_type="refresh")
    if not user_id:
        return None
    User = get_user_model()
    if not User.objects.filter(pk=user_id, is_active=True).exists():
        return None
    return {"access_token": make_access_token(user_id)}


def _create_api_key(user_id: int, label: str) -> dict:
    from django.contrib.auth import get_user_model

    from apiauth.models import ApiKey

    user = get_user_model().objects.get(pk=user_id)
    key, secret = ApiKey.generate(user, label=label)
    return {"key_id": key.key_id, "secret": secret, "label": key.label}


def _list_api_keys(user_id: int) -> list[dict]:
    from apiauth.models import ApiKey

    return [
        {"key_id": k.key_id, "label": k.label, "is_active": k.is_active,
         "created_at": k.created_at}
        for k in ApiKey.objects.filter(user_id=user_id)
    ]


def _revoke_api_key(user_id: int, key_id: str) -> bool:
    from apiauth.models import ApiKey

    updated = ApiKey.objects.filter(user_id=user_id, key_id=key_id).update(
        is_active=False
    )
    return updated > 0


# ── Endpoints ──────────────────────────────────────────────────────────────────
@router.post("/register", response_model=TokenResponse)
async def register(body: RegisterRequest):
    from django.conf import settings

    if not getattr(settings, "APIAUTH_OPEN_REGISTRATION", False):
        raise HTTPException(
            403, "Self-registration is disabled. Merchants are created by an admin."
        )
    result = await sync_to_async(_register)(body.name, str(body.email), body.password)
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    result = await sync_to_async(_login)(str(body.email), body.password)
    if not result:
        raise HTTPException(401, "invalid email or password")
    return result


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest):
    result = await sync_to_async(_refresh)(body.refresh_token)
    if not result:
        raise HTTPException(401, "invalid or expired refresh token")
    return result


@router.post("/api-keys", response_model=ApiKeyCreatedResponse)
async def create_api_key(body: ApiKeyCreateRequest, identity: dict = Depends(require_api_access)):
    return await sync_to_async(_create_api_key)(identity["user_id"], body.label)


@router.get("/api-keys", response_model=list[ApiKeyItem])
async def list_api_keys(identity: dict = Depends(require_api_access)):
    return await sync_to_async(_list_api_keys)(identity["user_id"])


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(key_id: str, identity: dict = Depends(require_api_access)):
    ok = await sync_to_async(_revoke_api_key)(identity["user_id"], key_id)
    if not ok:
        raise HTTPException(404, "key not found")
    return {"revoked": key_id}
