import json
import os

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apiauth.panel import manage_accounts_required

from .forms import MidasbuyAccountForm
from .models import MidasbuyAccount
from .services.login_service import login_account_and_persist


@manage_accounts_required
def account_list(request):
    accounts = MidasbuyAccount.objects.all()
    return render(request, "accounts/list.html", {"accounts": accounts})


@manage_accounts_required
def account_add(request):
    if request.method == "POST":
        form = MidasbuyAccountForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("account_list")
    else:
        form = MidasbuyAccountForm()
    return render(request, "accounts/add.html", {"form": form})


@manage_accounts_required
def account_delete(request, pk):
    acct = get_object_or_404(MidasbuyAccount, pk=pk)
    if request.method == "POST":
        acct.delete()
    return redirect("account_list")


@require_POST
@manage_accounts_required
def account_unflag(request, pk):
    """Clear a flag so the rotator uses this account again."""
    MidasbuyAccount.objects.filter(pk=pk).update(
        is_flagged=False, consecutive_errors=0, flagged_at=None
    )
    return redirect("account_list")


@require_POST
@manage_accounts_required
def account_login(request, pk):
    """Trigger Playwright login for this account."""
    acct = get_object_or_404(MidasbuyAccount, pk=pk)
    result = login_account_and_persist(acct)
    return JsonResponse({"success": result.success, "message": result.message})


@manage_accounts_required
def account_session_status(request, pk):
    """Quick JSON check of whether the session files exist."""
    acct = get_object_or_404(MidasbuyAccount, pk=pk)
    sd = acct.get_session_dir(str(settings.BASE_DIR))
    ssp = os.path.join(sd, "storage_state.json")
    cookie_path = os.path.join(sd, "cookies.json")

    cookies = []
    if os.path.exists(cookie_path):
        with open(cookie_path) as f:
            cookies = json.load(f)

    session_token = next(
        (c["value"] for c in cookies if c.get("name") == "session_token"), None
    )

    return JsonResponse({
        "account_id": acct.pk,
        "label": str(acct),
        "has_session_files": os.path.exists(ssp),
        "has_session_token": bool(session_token),
        "status": acct.get_status_display(),
        "last_login": str(acct.last_login) if acct.last_login else None,
    })
