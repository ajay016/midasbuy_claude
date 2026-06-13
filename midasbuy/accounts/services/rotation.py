"""
Account rotation + per-account rate limiting.

Clients never pick which Midasbuy bot account is used — the server does. This
module chooses one per operation:

  * Only logged-in accounts (status == 1) that aren't currently flagged are used.
  * Each account may make at most ``ACCOUNT_CAP_PER_MIN`` metered calls
    (redeem / code-status) per minute; counts live in Redis. Player lookups are
    not metered against the cap.
  * Accounts are picked round-robin; an account at its per-minute cap is skipped
    and the next one is tried. With a single account there is nothing to rotate
    to, so it stays on that one (cap not enforced — there's no alternative).
  * Continuous upstream errors flag an account (``report_result``); flagged
    accounts are skipped until a cooldown elapses, then given another chance.

All functions are SYNCHRONOUS (Django ORM + Redis) — call them via
``sync_to_async`` from the async API, or directly from the Celery worker.
"""
import logging
import time

from django.conf import settings
from django.utils import timezone

from accounts.models import MidasbuyAccount

logger = logging.getLogger("accounts")

ACCOUNT_CAP_PER_MIN = getattr(settings, "MIDASBUY_ACCOUNT_CAP_PER_MIN", 20)
FLAG_ERROR_THRESHOLD = getattr(settings, "MIDASBUY_ACCOUNT_FLAG_THRESHOLD", 5)
FLAG_COOLDOWN_SECONDS = getattr(settings, "MIDASBUY_ACCOUNT_FLAG_COOLDOWN", 10 * 60)


def _redis():
    import redis

    return redis.from_url(settings.CELERY_BROKER_URL)


def _minute() -> int:
    return int(time.time() // 60)


def available_accounts() -> list:
    """Logged-in, non-flagged accounts. Flagged accounts past the cooldown are
    auto-cleared and made available again."""
    now = timezone.now()
    out = []
    for a in MidasbuyAccount.objects.filter(status=1).order_by("id"):
        if a.is_flagged:
            if a.flagged_at and (now - a.flagged_at).total_seconds() >= FLAG_COOLDOWN_SECONDS:
                a.is_flagged = False
                a.consecutive_errors = 0
                a.flagged_at = None
                a.save(update_fields=["is_flagged", "consecutive_errors", "flagged_at"])
                out.append(a)
            # still in cooldown -> skip
        else:
            out.append(a)
    return out


def _reserve_if_capacity(r, account_id: int, minute: int) -> bool:
    key = f"acct:cap:{account_id}:{minute}"
    try:
        if int(r.get(key) or 0) >= ACCOUNT_CAP_PER_MIN:
            return False
        r.incr(key)
        r.expire(key, 90)
        return True
    except Exception:
        return True  # Redis hiccup -> don't block the operation


def _bump(account_id: int) -> None:
    try:
        r = _redis()
        key = f"acct:cap:{account_id}:{_minute()}"
        r.incr(key)
        r.expire(key, 90)
    except Exception:
        pass


def pick_account(metered: bool = True):
    """Return (MidasbuyAccount, None) to use, or (None, reason) if none available.

    ``metered`` counts the pick against the account's per-minute cap (use it for
    redeem / code-status; pass False for player lookups)."""
    accts = available_accounts()
    if not accts:
        return None, "No logged-in Midasbuy account is available."

    # Single account: nothing to rotate to — always use it.
    if len(accts) == 1:
        if metered:
            _bump(accts[0].id)
        return accts[0], None

    if not metered:
        # Spread lookups round-robin without cap enforcement.
        try:
            start = int(_redis().incr("acct:rr")) % len(accts)
        except Exception:
            start = 0
        return accts[start], None

    # Metered: round-robin, skipping accounts at their per-minute cap.
    try:
        r = _redis()
        minute = _minute()
        n = len(accts)
        start = int(r.incr("acct:rr")) % n
        for i in range(n):
            acct = accts[(start + i) % n]
            if _reserve_if_capacity(r, acct.id, minute):
                return acct, None
        return None, "All accounts are at their per-minute limit. Try again shortly."
    except Exception:
        logger.warning("[ROTATE] redis unavailable; using first available account")
        return accts[0], None


def report_result(account_id, success: bool) -> None:
    """Feed an operation's outcome back so repeated failures flag the account."""
    if account_id is None:
        return
    try:
        a = MidasbuyAccount.objects.get(pk=account_id)
    except MidasbuyAccount.DoesNotExist:
        return

    if success:
        if a.consecutive_errors or a.is_flagged:
            a.consecutive_errors = 0
            a.is_flagged = False
            a.flagged_at = None
            a.save(update_fields=["consecutive_errors", "is_flagged", "flagged_at"])
        return

    a.consecutive_errors = (a.consecutive_errors or 0) + 1
    fields = ["consecutive_errors"]
    if a.consecutive_errors >= FLAG_ERROR_THRESHOLD and not a.is_flagged:
        a.is_flagged = True
        a.flagged_at = timezone.now()
        fields += ["is_flagged", "flagged_at"]
        logger.warning("[ROTATE] account %s flagged after %s errors", account_id,
                       a.consecutive_errors)
    a.save(update_fields=fields)
