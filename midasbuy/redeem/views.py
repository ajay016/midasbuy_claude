from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import MidasbuyAccount
from apiauth.models import ApiKey, Merchant
from apiauth.panel import (
    current_merchant,
    login_merchant,
    logout_merchant,
    manage_users_required,
    merchant_required,
    order_required,
    use_api_required,
)


def login_view(request):
    """Panel login (email + password against the Merchant model)."""
    if current_merchant(request):
        return redirect("index")

    error = ""
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""
        merchant = Merchant.objects.filter(email=email, is_active=True).first()
        if merchant and merchant.check_password(password):
            login_merchant(request, merchant)
            return redirect("index")
        error = "Invalid email or password."

    return render(request, "redeem/login.html", {"error": error})


def logout_view(request):
    logout_merchant(request)
    return redirect("panel_login")


@merchant_required
def docs_view(request):
    from .api_docs import build_context

    ctx = build_context()
    ctx["merchant"] = current_merchant(request)
    return render(request, "redeem/docs.html", ctx)


@merchant_required
def index(request):
    """Dashboard landing. The redeem tool is shown only to users who may order;
    everyone else sees a short overview of what they can access."""
    from apiauth.security import make_access_token

    merchant = current_merchant(request)
    accounts = (
        MidasbuyAccount.objects.filter(status=1)  # logged-in accounts only
        if merchant.allowed_to_order
        else MidasbuyAccount.objects.none()
    )
    return render(
        request,
        "redeem/index.html",
        {
            "accounts": accounts,
            "merchant": merchant,
            "api_token": make_access_token(merchant.id),
        },
    )


# ── Team management (admin only) ───────────────────────────────────────────────
_ROLE_DEFAULT_CAPS = {
    Merchant.ROLE_ADMIN: {"can_order": True, "can_manage_accounts": True, "can_use_api": True},
    Merchant.ROLE_STAFF: {"can_order": True, "can_manage_accounts": False, "can_use_api": False},
    Merchant.ROLE_CLIENT: {"can_order": True, "can_manage_accounts": False, "can_use_api": True},
}


@manage_users_required
def team_list(request):
    merchant = current_merchant(request)
    users = Merchant.objects.all()
    return render(request, "redeem/team_list.html", {"merchant": merchant, "users": users})


def _read_user_form(request):
    """Pull and lightly validate the shared add/edit form fields."""
    role = request.POST.get("role") or Merchant.ROLE_CLIENT
    if role not in dict(Merchant.ROLE_CHOICES):
        role = Merchant.ROLE_CLIENT
    return {
        "name": (request.POST.get("name") or "").strip(),
        "email": (request.POST.get("email") or "").strip().lower(),
        "role": role,
        "can_order": bool(request.POST.get("can_order")),
        "can_manage_accounts": bool(request.POST.get("can_manage_accounts")),
        "can_use_api": bool(request.POST.get("can_use_api")),
        "password": request.POST.get("password") or "",
    }


@manage_users_required
def team_add(request):
    merchant = current_merchant(request)
    error = ""
    if request.method == "POST":
        data = _read_user_form(request)
        if not data["name"] or not data["email"]:
            error = "Name and email are required."
        elif len(data["password"]) < 8:
            error = "Password must be at least 8 characters."
        elif Merchant.objects.filter(email__iexact=data["email"]).exists():
            error = "A user with that email already exists."
        else:
            user = Merchant(
                name=data["name"], email=data["email"], role=data["role"],
                can_order=data["can_order"],
                can_manage_accounts=data["can_manage_accounts"],
                can_use_api=data["can_use_api"],
            )
            user.set_password(data["password"])
            user.save()
            messages.success(request, f"Created {user.role_label.lower()} “{user.name}”.")
            return redirect("team_edit", pk=user.pk)

    return render(
        request,
        "redeem/team_form.html",
        {"merchant": merchant, "error": error, "obj": None,
         "role_defaults": _ROLE_DEFAULT_CAPS},
    )


@manage_users_required
def team_edit(request, pk):
    merchant = current_merchant(request)
    user = get_object_or_404(Merchant, pk=pk)
    error = ""
    secret_once = request.session.pop("secret_once", None)

    if request.method == "POST":
        data = _read_user_form(request)
        if not data["name"] or not data["email"]:
            error = "Name and email are required."
        elif Merchant.objects.filter(email__iexact=data["email"]).exclude(pk=user.pk).exists():
            error = "Another user already uses that email."
        else:
            user.name = data["name"]
            user.email = data["email"]
            user.role = data["role"]
            user.can_order = data["can_order"]
            user.can_manage_accounts = data["can_manage_accounts"]
            user.can_use_api = data["can_use_api"]
            user.is_active = bool(request.POST.get("is_active"))
            if data["password"]:
                if len(data["password"]) < 8:
                    error = "Password must be at least 8 characters."
                else:
                    user.set_password(data["password"])
            if not error:
                user.save()
                messages.success(request, "Saved.")
                return redirect("team_edit", pk=user.pk)

    return render(
        request,
        "redeem/team_form.html",
        {"merchant": merchant, "error": error, "obj": user,
         "keys": user.api_keys.all(), "secret_once": secret_once,
         "role_defaults": _ROLE_DEFAULT_CAPS},
    )


@require_POST
@manage_users_required
def team_delete(request, pk):
    merchant = current_merchant(request)
    user = get_object_or_404(Merchant, pk=pk)
    if user.pk == merchant.pk:
        messages.error(request, "You can't delete your own account.")
        return redirect("team_edit", pk=pk)
    name = user.name
    user.delete()
    messages.success(request, f"Deleted “{name}”.")
    return redirect("team_list")


@require_POST
@manage_users_required
def team_apikey_create(request, pk):
    user = get_object_or_404(Merchant, pk=pk)
    if not user.allowed_to_use_api:
        messages.error(request, "Enable API access for this user before issuing a key.")
        return redirect("team_edit", pk=pk)
    label = (request.POST.get("label") or "").strip()
    key, secret = ApiKey.generate(user, label=label)
    # Stash the one-time secret in the session so a redirect can display it once.
    request.session["secret_once"] = {"key_id": key.key_id, "secret": secret, "label": key.label}
    messages.success(request, "API key created — copy the secret now, it won't be shown again.")
    return redirect("team_edit", pk=pk)


@require_POST
@manage_users_required
def team_apikey_revoke(request, pk, key_id):
    user = get_object_or_404(Merchant, pk=pk)
    ApiKey.objects.filter(merchant=user, key_id=key_id).update(is_active=False)
    messages.success(request, "Key revoked.")
    return redirect("team_edit", pk=pk)


# ── Self-service API keys (any user with API access) ───────────────────────────
@use_api_required
def my_api_keys(request):
    merchant = current_merchant(request)
    secret_once = request.session.pop("secret_once", None)
    return render(
        request,
        "redeem/api_keys.html",
        {"merchant": merchant, "keys": merchant.api_keys.all(), "secret_once": secret_once},
    )


@require_POST
@use_api_required
def my_apikey_create(request):
    merchant = current_merchant(request)
    label = (request.POST.get("label") or "").strip()
    key, secret = ApiKey.generate(merchant, label=label)
    request.session["secret_once"] = {"key_id": key.key_id, "secret": secret, "label": key.label}
    messages.success(request, "API key created — copy the secret now, it won't be shown again.")
    return redirect("my_api_keys")


@require_POST
@use_api_required
def my_apikey_revoke(request, key_id):
    merchant = current_merchant(request)
    ApiKey.objects.filter(merchant=merchant, key_id=key_id).update(is_active=False)
    messages.success(request, "Key revoked.")
    return redirect("my_api_keys")
