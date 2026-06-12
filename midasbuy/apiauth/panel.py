"""
Session-based merchant auth for the HTML panel.

The /api endpoints use JWT/HMAC (stateless). The panel pages instead use a normal
Django session: on login we store the merchant id, and panel views mint a
short-lived API JWT for that merchant so the page's JS can call /api.
"""
import functools

from django.contrib import messages
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


def capability_required(attr: str, message: str):
    """Build a decorator that requires a Merchant.allowed_to_* property to be True.

    Enforced server-side: hiding a menu item is not enough — a user who guesses
    the URL is still bounced here. Logged-out users go to login; logged-in users
    without the capability are sent back to the dashboard with an error.
    """
    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            merchant = current_merchant(request)
            if merchant is None:
                return redirect("panel_login")
            if not getattr(merchant, attr, False):
                messages.error(request, message)
                return redirect("index")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


order_required = capability_required(
    "allowed_to_order", "You don't have permission to place orders."
)
manage_accounts_required = capability_required(
    "allowed_to_manage_accounts", "You don't have permission to manage bot accounts."
)
manage_users_required = capability_required(
    "allowed_to_manage_users", "Only admins can manage users."
)
use_api_required = capability_required(
    "allowed_to_use_api", "API access isn't enabled on your account."
)
