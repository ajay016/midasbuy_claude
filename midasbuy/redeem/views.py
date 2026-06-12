import secrets

from django.shortcuts import render

from accounts.models import MidasbuyAccount


def _panel_api_token() -> str:
    """
    Mint a short-lived JWT so the panel's browser JS can call the now-authenticated
    /api endpoints. The panel runs as a dedicated internal Merchant. When real
    merchant login lands, mint the logged-in merchant's token here instead.
    """
    from apiauth.models import Merchant
    from apiauth.security import make_access_token

    merchant, created = Merchant.objects.get_or_create(
        email="panel@system.local", defaults={"name": "Panel"},
    )
    if created:
        merchant.set_password(secrets.token_urlsafe(32))
        merchant.save(update_fields=["password"])
    return make_access_token(merchant.id)


def index(request):
    accounts = MidasbuyAccount.objects.filter(status=1)  # logged-in accounts only
    return render(
        request,
        "redeem/index.html",
        {"accounts": accounts, "api_token": _panel_api_token()},
    )
