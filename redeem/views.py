from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import MidasbuyAccount
from apiauth.models import ApiKey
from billing.models import Subscription
from apiauth.panel import (
    manage_users_required,
    order_required,
    panel_login_required,
    use_api_required,
)

User = get_user_model()


def login_view(request):
    """Panel login (email + password) via Django's auth."""
    if request.user.is_authenticated:
        return redirect("index")

    error = ""
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""
        user = authenticate(request, username=email, password=password)
        if user is not None:
            auth_login(request, user)
            return redirect("index")
        error = "Invalid email or password."

    return render(request, "redeem/login.html", {"error": error})


def logout_view(request):
    auth_logout(request)
    return redirect("panel_login")


@panel_login_required
def docs_view(request):
    from .api_docs import build_context

    ctx = build_context()
    return render(request, "redeem/docs.html", ctx)


@panel_login_required
def index(request):
    """Dashboard landing. The redeem tool is shown only to users who may order;
    everyone else sees a short overview of what they can access."""
    from apiauth.security import make_access_token

    user = request.user
    accounts = (
        MidasbuyAccount.objects.filter(status=1)  # logged-in accounts only
        if user.allowed_to_order
        else MidasbuyAccount.objects.none()
    )
    # Clients see their own quota usage; admins/staff are unlimited.
    subs = None
    if user.role == User.ROLE_CLIENT:
        summary = Subscription.summary_for(user)
        subs = [(Subscription.PLAN_PANEL, summary[Subscription.PLAN_PANEL]),
                (Subscription.PLAN_API, summary[Subscription.PLAN_API])]
    return render(
        request,
        "redeem/index.html",
        {"accounts": accounts, "api_token": make_access_token(user.id), "subs": subs},
    )


@order_required
def bulk_page(request):
    """Panel UI for bulk lookups / code-status / redeem. Calls /api/bulk/* with a
    short-lived JWT, so usage is metered against the user's PANEL plan."""
    from apiauth.security import make_access_token

    user = request.user
    accounts = MidasbuyAccount.objects.filter(status=1)
    return render(
        request,
        "redeem/bulk.html",
        {"accounts": accounts, "api_token": make_access_token(user.id)},
    )


# ── Team management (admin only) ───────────────────────────────────────────────
_ROLE_DEFAULT_CAPS = {
    User.ROLE_ADMIN: {"can_order": True, "can_manage_accounts": True, "can_use_api": True},
    User.ROLE_STAFF: {"can_order": True, "can_manage_accounts": False, "can_use_api": False},
    User.ROLE_CLIENT: {"can_order": True, "can_manage_accounts": False, "can_use_api": True},
}


@manage_users_required
def team_list(request):
    return render(request, "redeem/team_list.html", {"users": User.objects.all()})


def _read_user_form(request):
    """Pull and lightly validate the shared add/edit form fields."""
    role = request.POST.get("role") or User.ROLE_CLIENT
    if role not in dict(User.ROLE_CHOICES):
        role = User.ROLE_CLIENT
    try:
        rate = int(request.POST.get("rate_limit_per_min"))
    except (TypeError, ValueError):
        rate = 20
    return {
        "name": (request.POST.get("name") or "").strip(),
        "email": (request.POST.get("email") or "").strip().lower(),
        "role": role,
        "can_order": bool(request.POST.get("can_order")),
        "can_manage_accounts": bool(request.POST.get("can_manage_accounts")),
        "can_use_api": bool(request.POST.get("can_use_api")),
        "rate_limit_per_min": max(0, rate),
        "password": request.POST.get("password") or "",
    }


def _apply_role_flags(obj, data):
    obj.role = data["role"]
    obj.can_order = data["can_order"]
    obj.can_manage_accounts = data["can_manage_accounts"]
    obj.can_use_api = data["can_use_api"]
    # Admins manage the panel/admin site; staff & clients do not.
    obj.is_staff = data["role"] == User.ROLE_ADMIN
    obj.is_superuser = data["role"] == User.ROLE_ADMIN
    obj.rate_limit_per_min = data["rate_limit_per_min"]


@manage_users_required
def team_add(request):
    error = ""
    if request.method == "POST":
        data = _read_user_form(request)
        if not data["name"] or not data["email"]:
            error = "Name and email are required."
        elif len(data["password"]) < 8:
            error = "Password must be at least 8 characters."
        elif User.objects.filter(email__iexact=data["email"]).exists():
            error = "A user with that email already exists."
        else:
            obj = User(name=data["name"], email=data["email"])
            _apply_role_flags(obj, data)
            obj.set_password(data["password"])
            obj.save()
            messages.success(request, f"Created {obj.role_label.lower()} “{obj.name}”.")
            return redirect("team_edit", pk=obj.pk)

    return render(
        request,
        "redeem/team_form.html",
        {"error": error, "obj": None, "role_defaults": _ROLE_DEFAULT_CAPS},
    )


@manage_users_required
def team_edit(request, pk):
    obj = get_object_or_404(User, pk=pk)
    error = ""
    secret_once = request.session.pop("secret_once", None)

    if request.method == "POST":
        data = _read_user_form(request)
        if not data["name"] or not data["email"]:
            error = "Name and email are required."
        elif User.objects.filter(email__iexact=data["email"]).exclude(pk=obj.pk).exists():
            error = "Another user already uses that email."
        else:
            obj.name = data["name"]
            obj.email = data["email"]
            _apply_role_flags(obj, data)
            obj.is_active = bool(request.POST.get("is_active"))
            if data["password"]:
                if len(data["password"]) < 8:
                    error = "Password must be at least 8 characters."
                else:
                    obj.set_password(data["password"])
            if not error:
                obj.save()
                messages.success(request, "Saved.")
                return redirect("team_edit", pk=obj.pk)

    summary = Subscription.summary_for(obj)
    plan_rows = [(Subscription.PLAN_PANEL, summary[Subscription.PLAN_PANEL]),
                 (Subscription.PLAN_API, summary[Subscription.PLAN_API])]
    return render(
        request,
        "redeem/team_form.html",
        {"error": error, "obj": obj, "keys": obj.api_keys.all(),
         "secret_once": secret_once, "role_defaults": _ROLE_DEFAULT_CAPS,
         "plan_rows": plan_rows},
    )


@require_POST
@manage_users_required
def team_delete(request, pk):
    obj = get_object_or_404(User, pk=pk)
    if obj.pk == request.user.pk:
        messages.error(request, "You can't delete your own account.")
        return redirect("team_edit", pk=pk)
    name = obj.name
    obj.delete()
    messages.success(request, f"Deleted “{name}”.")
    return redirect("team_list")


@require_POST
@manage_users_required
def team_apikey_create(request, pk):
    obj = get_object_or_404(User, pk=pk)
    if not obj.allowed_to_use_api:
        messages.error(request, "Enable API access for this user before issuing a key.")
        return redirect("team_edit", pk=pk)
    label = (request.POST.get("label") or "").strip()
    key, secret = ApiKey.generate(obj, label=label)
    # Stash the one-time secret in the session so a redirect can display it once.
    request.session["secret_once"] = {"key_id": key.key_id, "secret": secret, "label": key.label}
    messages.success(request, "API key created — copy the secret now, it won't be shown again.")
    return redirect("team_edit", pk=pk)


@require_POST
@manage_users_required
def team_apikey_revoke(request, pk, key_id):
    obj = get_object_or_404(User, pk=pk)
    ApiKey.objects.filter(user=obj, key_id=key_id).update(is_active=False)
    messages.success(request, "Key revoked.")
    return redirect("team_edit", pk=pk)


@require_POST
@manage_users_required
def team_subscription_set(request, pk, plan):
    """Grant or renew a Panel/API subscription for a user (admin only)."""
    obj = get_object_or_404(User, pk=pk)
    if plan not in dict(Subscription.PLAN_CHOICES):
        messages.error(request, "Unknown plan.")
        return redirect("team_edit", pk=pk)
    try:
        limit = int(request.POST.get("request_limit") or Subscription._meta.get_field("request_limit").default)
    except (TypeError, ValueError):
        limit = 5000
    limit = max(1, limit)
    Subscription.grant(obj, plan, request_limit=limit)
    messages.success(request, f"{plan.title()} plan granted ({limit} requests / 30 days).")
    return redirect("team_edit", pk=pk)


@require_POST
@manage_users_required
def team_subscription_revoke(request, pk, plan):
    obj = get_object_or_404(User, pk=pk)
    Subscription.objects.filter(user=obj, plan=plan).update(is_active=False)
    messages.success(request, f"{plan.title()} plan deactivated.")
    return redirect("team_edit", pk=pk)


# ── Self-service API keys (any user with API access) ───────────────────────────
@use_api_required
def my_api_keys(request):
    secret_once = request.session.pop("secret_once", None)
    return render(
        request,
        "redeem/api_keys.html",
        {"keys": request.user.api_keys.all(), "secret_once": secret_once},
    )


@require_POST
@use_api_required
def my_apikey_create(request):
    label = (request.POST.get("label") or "").strip()
    key, secret = ApiKey.generate(request.user, label=label)
    request.session["secret_once"] = {"key_id": key.key_id, "secret": secret, "label": key.label}
    messages.success(request, "API key created — copy the secret now, it won't be shown again.")
    return redirect("my_api_keys")


@require_POST
@use_api_required
def my_apikey_revoke(request, key_id):
    ApiKey.objects.filter(user=request.user, key_id=key_id).update(is_active=False)
    messages.success(request, "Key revoked.")
    return redirect("my_api_keys")
