"""
Partner self-service API (mounted under /api/partner).

A partner resells our API to their own end-customers. These endpoints let a partner
manage *their own* clients and each client's subscription, entirely over the API —
no panel login needed. Every route requires a partner (or admin) caller, and all
queries are scoped to the caller, so a partner can only ever see/touch clients they
own.

Once a client exists, the partner calls the normal redeem/lookup endpoints with
their own API key plus the `X-Client-Id: <client_ref>` header to attribute (and
meter) the request to that client.
"""
import logging
import secrets
from typing import Optional

from asgiref.sync import sync_to_async
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .dependencies import require_partner

logger = logging.getLogger("api")

router = APIRouter(prefix="/partner", tags=["partner"],
                   dependencies=[Depends(require_partner)])


# ── Schemas ────────────────────────────────────────────────────────────────────
class ClientCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: Optional[str] = None            # optional; a synthetic one is made if blank
    allowed_ips: str = ""                  # optional IP/CIDR allow-list for this client
    request_limit: Optional[int] = Field(default=None, ge=1)  # api quota per period
    unlimited: bool = False                # grant an uncapped (still metered) api plan


class ClientUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    allowed_ips: Optional[str] = None
    is_active: Optional[bool] = None


class SubscriptionSet(BaseModel):
    plan: str = "api"
    request_limit: Optional[int] = Field(default=None, ge=1)
    unlimited: bool = False


# ── Sync DB helpers (Django ORM is sync; call via sync_to_async) ───────────────
def _sub_dict(s) -> Optional[dict]:
    if not s:
        return None
    return {
        "active": s.is_active,
        "used": s.used,
        "limit": s.request_limit,          # null = unlimited
        "unlimited": s.is_unlimited,
        "period_end": s.period_end.isoformat() if s.period_end else None,
    }


def _client_dict(user) -> dict:
    from billing.models import Subscription

    summary = Subscription.summary_for(user)
    return {
        "client_ref": user.client_ref,     # the X-Client-Id value to send on calls
        "name": user.name,
        "email": user.email,
        "allowed_ips": user.allowed_ips,
        "is_active": user.is_active,
        "subscriptions": {plan: _sub_dict(s) for plan, s in summary.items()},
    }


def _owned_qs(partner_id: int):
    from apiauth.models import User

    return User.objects.filter(role=User.ROLE_CLIENT, partner_id=partner_id)


def _list_clients_sync(partner_id: int) -> list:
    return [_client_dict(u) for u in _owned_qs(partner_id)]


def _get_owned_or_404(partner_id: int, client_ref: str):
    user = _owned_qs(partner_id).filter(client_ref=client_ref).first()
    if user is None:
        raise HTTPException(status_code=404, detail="No such client under your account.")
    return user


def _grant_sub(user, plan: str, request_limit, unlimited: bool):
    from billing.models import Subscription

    if plan not in dict(Subscription.PLAN_CHOICES):
        raise HTTPException(status_code=400, detail="Unknown plan.")
    limit = None if unlimited else request_limit
    Subscription.grant(user, plan, request_limit=limit)


def _create_client_sync(partner_id: int, data: ClientCreate) -> dict:
    from apiauth.models import User

    email = (data.email or "").strip().lower()
    if email:
        if User.objects.filter(email__iexact=email).exists():
            raise HTTPException(status_code=409, detail="That email is already in use.")
    else:
        # Sub-clients don't log in, so a unique synthetic address is fine.
        email = f"client+{secrets.token_hex(8)}@partner-{partner_id}.local"

    user = User(
        name=data.name.strip(),
        email=email,
        role=User.ROLE_CLIENT,
        partner_id=partner_id,
        allowed_ips=data.allowed_ips or "",
        can_order=True,        # the partner places orders on this client's behalf
        can_use_api=False,     # the partner calls with their own key + X-Client-Id
    )
    user.set_unusable_password()
    user.save()

    if data.unlimited or data.request_limit is not None:
        _grant_sub(user, "api", data.request_limit, data.unlimited)
    return _client_dict(user)


def _update_client_sync(partner_id: int, client_ref: str, data: ClientUpdate) -> dict:
    user = _get_owned_or_404(partner_id, client_ref)
    if data.name is not None:
        user.name = data.name.strip()
    if data.allowed_ips is not None:
        user.allowed_ips = data.allowed_ips
    if data.is_active is not None:
        user.is_active = data.is_active
    user.save()
    return _client_dict(user)


def _set_subscription_sync(partner_id: int, client_ref: str, data: SubscriptionSet) -> dict:
    user = _get_owned_or_404(partner_id, client_ref)
    _grant_sub(user, data.plan, data.request_limit, data.unlimited)
    return _client_dict(user)


def _get_client_sync(partner_id: int, client_ref: str) -> dict:
    return _client_dict(_get_owned_or_404(partner_id, client_ref))


# ── Routes ─────────────────────────────────────────────────────────────────────
@router.get("/clients")
async def list_clients(identity: dict = Depends(require_partner)):
    """List the clients you own, each with their usage meters."""
    clients = await sync_to_async(_list_clients_sync)(identity["user_id"])
    return {"clients": clients, "count": len(clients)}


@router.post("/clients", status_code=201)
async def create_client(body: ClientCreate, identity: dict = Depends(require_partner)):
    """Create a client under you. The returned `client_ref` is the value you send
    as `X-Client-Id` on redeem/lookup calls to attribute usage to this client."""
    return await sync_to_async(_create_client_sync)(identity["user_id"], body)


@router.get("/clients/{client_ref}")
async def get_client(client_ref: str, identity: dict = Depends(require_partner)):
    return await sync_to_async(_get_client_sync)(identity["user_id"], client_ref)


@router.patch("/clients/{client_ref}")
async def update_client(client_ref: str, body: ClientUpdate,
                        identity: dict = Depends(require_partner)):
    """Update a client's name, IP allow-list, or active state."""
    return await sync_to_async(_update_client_sync)(identity["user_id"], client_ref, body)


@router.post("/clients/{client_ref}/subscription")
async def set_subscription(client_ref: str, body: SubscriptionSet,
                           identity: dict = Depends(require_partner)):
    """Grant or renew a client's subscription (custom limit, or unlimited)."""
    return await sync_to_async(_set_subscription_sync)(identity["user_id"], client_ref, body)
