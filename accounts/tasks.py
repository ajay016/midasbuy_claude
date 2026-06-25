"""
Scheduled account maintenance tasks.

Bot-account logins (the Midasbuy storage_state / cookies) go stale over time, so we
refresh them on a schedule instead of waiting for them to expire mid-redeem. The
beat task `refresh_account_logins` walks the logged-in accounts ONE AT A TIME: each
is marked "re-logging in" (so rotation skips just that one) while it's refreshed, so
the remaining accounts keep serving redemptions the whole time.
"""
import logging

from celery import shared_task

from accounts.models import MidasbuyAccount
from accounts.services.rotation import clear_relogin, mark_relogin

logger = logging.getLogger("accounts")


@shared_task(ignore_result=True)
def relogin_account(account_id: int) -> bool:
    """Re-login ONE bot account. Marks it in-relogin (rotation skips only this one),
    refreshes its session, then clears the marker. Returns True on success."""
    from accounts.services.login_service import login_account_and_persist

    try:
        account = MidasbuyAccount.objects.get(pk=account_id)
    except MidasbuyAccount.DoesNotExist:
        logger.warning("[RELOGIN] account=%s not found", account_id)
        return False

    mark_relogin(account_id)
    try:
        result = login_account_and_persist(account)
        logger.info("[RELOGIN] account=%s success=%s", account_id, result.success)
        return bool(result.success)
    except Exception:
        logger.exception("[RELOGIN] account=%s crashed", account_id)
        return False
    finally:
        clear_relogin(account_id)


@shared_task(ignore_result=True)
def refresh_account_logins() -> int:
    """Beat entrypoint: refresh every logged-in account, stalest first, ONE AT A TIME
    (sequentially in this task) so at most one account is ever out of rotation. Runs
    on the schedule in celery.py. Returns how many were refreshed."""
    accounts = list(
        MidasbuyAccount.objects.filter(status=1).order_by("last_login_at", "id")
    )
    if not accounts:
        logger.info("[RELOGIN] no logged-in accounts to refresh")
        return 0

    logger.info("[RELOGIN] refreshing %d account login(s), one at a time", len(accounts))
    for account in accounts:
        # Synchronous so the next account doesn't start until this one rejoins
        # rotation — guarantees only one account is ever mid-relogin.
        relogin_account.run(account.id)
    return len(accounts)
