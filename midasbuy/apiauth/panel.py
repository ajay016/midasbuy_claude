"""
Session-based merchant auth for the HTML panel.

The /api endpoints use JWT/HMAC (stateless). The panel pages instead use a normal
Django session: on login we store the merchant id, and panel views mint a
short-lived API JWT for that merchant so the page's JS can call /api.
"""
import functools

from django.conf import settings
from django.shortcuts import redirect

SESSION_KEY = "merchant_id"


def login_merchant(request, merchant) -> None:
    request.session[SESSION_KEY] = merchant.id


def logout_merchant(request) -> None:
    request.session.pop(SESSION_KEY, None)


def current_merchant(request):
    """Return the logged-in Merchant, or None."""
    from .models import Merchant

    mid = request.session.get(SESSION_KEY)
    if not mid:
        return None
    return Merchant.objects.filter(pk=mid, is_active=True).first()


def merchant_required(view_func):
    """Redirect to the login page if no merchant is logged in."""
    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if current_merchant(request) is None:
            return redirect("panel_login")
        return view_func(request, *args, **kwargs)

    return _wrapped


# Convenience for the testing phase: a known merchant so you can log in and
# exercise lookup/redeem immediately. Only ever created when DEBUG is on.
DEV_EMAIL = "test@local"
DEV_PASSWORD = "test1234"


def ensure_dev_merchant():
    """In DEBUG, make sure a test merchant exists. Returns (email, password) or None."""
    if not settings.DEBUG:
        return None
    from .models import Merchant

    merchant, created = Merchant.objects.get_or_create(
        email=DEV_EMAIL, defaults={"name": "Test Merchant"},
    )
    if created or not merchant.check_password(DEV_PASSWORD):
        merchant.set_password(DEV_PASSWORD)
        merchant.is_active = True
        merchant.save()
    return DEV_EMAIL, DEV_PASSWORD
