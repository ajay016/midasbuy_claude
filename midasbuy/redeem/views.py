from django.shortcuts import redirect, render

from accounts.models import MidasbuyAccount
from apiauth.panel import (
    current_merchant,
    ensure_dev_merchant,
    login_merchant,
    logout_merchant,
    merchant_required,
)


def login_view(request):
    """Merchant login for the panel (email + password against the Merchant model)."""
    if current_merchant(request):
        return redirect("index")

    dev = ensure_dev_merchant()  # only returns creds in DEBUG
    error = ""
    if request.method == "POST":
        from apiauth.models import Merchant

        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""
        merchant = Merchant.objects.filter(email=email, is_active=True).first()
        if merchant and merchant.check_password(password):
            login_merchant(request, merchant)
            return redirect("index")
        error = "Invalid email or password."

    return render(request, "redeem/login.html", {"error": error, "dev": dev})


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
    from apiauth.security import make_access_token

    merchant = current_merchant(request)
    accounts = MidasbuyAccount.objects.filter(status=1)  # logged-in accounts only
    return render(
        request,
        "redeem/index.html",
        {
            "accounts": accounts,
            "merchant": merchant,
            "api_token": make_access_token(merchant.id),
        },
    )
