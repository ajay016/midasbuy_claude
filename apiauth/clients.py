"""
Client-management service — the single place that creates/edits a partner's clients
and reads their usage. BOTH the partner API (api/partner_routes.py) and the panel
(redeem/views.py) call these, so the two can never drift.

A "client" is a User with role=client and a `partner` FK. It never logs in or holds
its own API key; the partner calls our API with their own key plus
`X-Client-Id: <client_ref>` and we meter the request against the client's plan.
"""
import secrets

from django.db.models import Count, Sum

from billing.models import Subscription

from .models import User


class ClientError(ValueError):
    """Raised for caller-fixable problems (e.g. duplicate email, unknown plan).
    API turns it into 4xx; the panel shows it as a form error."""


# ── Queries ────────────────────────────────────────────────────────────────────
def owned_clients(partner):
    """All clients owned by `partner` (a User), newest first."""
    return (User.objects.filter(role=User.ROLE_CLIENT, partner=partner)
            .order_by("-created_at"))


def get_owned_client(partner, client_ref):
    """One owned client by its client_ref, or None."""
    return owned_clients(partner).filter(client_ref=client_ref).first()


# ── Mutations ──────────────────────────────────────────────────────────────────
def set_client_subscription(client, plan="api", request_limit=None, unlimited=False):
    """Grant/renew a client's plan. unlimited OR a blank limit -> uncapped."""
    if plan not in dict(Subscription.PLAN_CHOICES):
        raise ClientError("Unknown plan.")
    limit = None if unlimited else request_limit
    return Subscription.grant(client, plan, request_limit=limit)


def create_client(partner, *, name, email=None, allowed_ips="",
                  request_limit=None, unlimited=False):
    """Create a client under `partner`. Returns the new User.

    Sub-clients don't log in, so a unique synthetic email is generated when none is
    given, and the password is set unusable."""
    name = (name or "").strip()
    if not name:
        raise ClientError("Client name is required.")
    email = (email or "").strip().lower()
    if email:
        if User.objects.filter(email__iexact=email).exists():
            raise ClientError("That email is already in use.")
    else:
        email = f"client+{secrets.token_hex(8)}@partner-{partner.id}.local"

    client = User(
        name=name, email=email, role=User.ROLE_CLIENT, partner=partner,
        allowed_ips=allowed_ips or "",
        can_order=True,      # the partner places orders on this client's behalf
        can_use_api=False,   # the partner calls with its own key + X-Client-Id
    )
    client.set_unusable_password()
    client.save()

    if unlimited or request_limit is not None:
        set_client_subscription(client, "api", request_limit, unlimited)
    return client


def update_client(client, *, name=None, allowed_ips=None, is_active=None):
    """Patch a client's name / IP allow-list / active flag (only given fields)."""
    if name is not None:
        name = name.strip()
        if not name:
            raise ClientError("Client name can't be empty.")
        client.name = name
    if allowed_ips is not None:
        client.allowed_ips = allowed_ips
    if is_active is not None:
        client.is_active = is_active
    client.save()
    return client


# ── Admin aggregates (for the grouped overview, computed without N+1) ───────────
def partners_with_usage():
    """Every partner annotated with their client count and combined API usage —
    three small queries total, no per-row DB hits."""
    partners = list(User.objects.filter(role=User.ROLE_PARTNER).order_by("name"))

    counts = {r["partner"]: r["n"] for r in (
        User.objects.filter(role=User.ROLE_CLIENT, partner__isnull=False)
        .values("partner").annotate(n=Count("id")))}

    used = {r["user__partner"]: r["used"] for r in (
        Subscription.objects.filter(plan=Subscription.PLAN_API,
                                    user__role=User.ROLE_CLIENT,
                                    user__partner__isnull=False)
        .values("user__partner").annotate(used=Sum("used")))}

    for p in partners:
        p.client_count = counts.get(p.id, 0)
        p.combined_api_used = used.get(p.id, 0) or 0
    return partners


def direct_clients():
    """Clients NOT under any partner (i.e. our own direct clients)."""
    return (User.objects.filter(role=User.ROLE_CLIENT, partner__isnull=True)
            .order_by("name"))
