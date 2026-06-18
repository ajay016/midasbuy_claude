"""
Panel access control, built on Django's own auth.

Since `User` is now `AUTH_USER_MODEL`, the panel uses standard Django session login
(`django.contrib.auth.login`), so `request.user` is the logged-in `User`. These
decorators add the capability checks on top of authentication.
"""
import functools

from django.contrib import messages
from django.shortcuts import redirect


def panel_login_required(view_func):
    """Redirect anonymous users to the login page."""
    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("panel_login")
        return view_func(request, *args, **kwargs)

    return _wrapped


def capability_required(attr: str, message: str):
    """Build a decorator requiring a User.allowed_to_* property to be True.

    Enforced server-side: hiding a menu item is not enough — a user who guesses
    the URL is still bounced here. Anonymous users go to login; authenticated users
    without the capability are sent back to the dashboard with an error.
    """
    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect("panel_login")
            if not getattr(request.user, attr, False):
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
manage_clients_required = capability_required(
    "allowed_to_manage_clients", "Only partners and admins can manage clients."
)
use_api_required = capability_required(
    "allowed_to_use_api", "API access isn't enabled on your account."
)
