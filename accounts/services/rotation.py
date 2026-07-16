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
# How long a "re-login in progress" marker lives. It auto-expires so a crashed
# re-login can never strand an account out of rotation forever.
RELOGIN_MARKER_TTL = getattr(settings, "MIDASBUY_RELOGIN_MARKER_TTL", 600)


def _redis():
    import redis

    # Short timeouts so a DOWN/unreachable Redis fails in ~150ms instead of
    # blocking the request for seconds on a TCP connect timeout.
    return redis.from_url(
        settings.CELERY_BROKER_URL,
        socket_connect_timeout=0.15,
        socket_timeout=0.15,
    )


# ── Re-login marker ─────────────────────────────────────────────────────────────
# While an account is being re-logged-in (storage_state torn down + rebuilt) it is
# briefly unusable, so rotation must skip ONLY that one account for those seconds —
# every other account keeps serving. The marker lives in Redis (shared between the
# web process that rotates and the Celery worker that re-logs-in) and self-expires.
def _relogin_key(account_id: int) -> str:
    return f"acct:relogin:{account_id}"


def mark_relogin(account_id: int, ttl: int = None) -> None:
    try:
        _redis().set(_relogin_key(account_id), "1", ex=ttl or RELOGIN_MARKER_TTL)
    except Exception:
        pass  # Redis hiccup — worst case rotation just doesn't skip it.


def clear_relogin(account_id: int) -> None:
    try:
        _redis().delete(_relogin_key(account_id))
    except Exception:
        pass


def _accounts_in_relogin(ids: list) -> set:
    """The subset of account ids currently being re-logged-in (one Redis round-trip)."""
    if not ids:
        return set()
    try:
        vals = _redis().mget([_relogin_key(i) for i in ids])
        return {i for i, v in zip(ids, vals) if v}
    except Exception:
        return set()  # Redis down -> skip nobody (fail open).


def _minute() -> int:
    return int(time.time() // 60)


def available_accounts() -> list:
    """Logged-in, non-flagged accounts that aren't mid-relogin. Flagged accounts past
    the cooldown are auto-cleared and made available again."""
    now = timezone.now()
    candidates = list(MidasbuyAccount.objects.filter(status=1).order_by("id"))
    relogin = _accounts_in_relogin([a.id for a in candidates])
    out = []
    for a in candidates:
        if a.id in relogin:
            continue  # currently re-logging in — skip just this one
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
        # Player lookups are read-only and uncapped. Do NOT round-robin them across
        # every account: that spreads lookup traffic thin, so each account's warm
        # browser session sits idle longer between hits and goes cold — and the
        # lookup that lands on a cold session pays a slow re-warm. That's exactly
        # why adding more accounts made lookups SLOWER, not faster. Pin lookups to
        # ONE stable account so its session stays hot regardless of account count.
        return accts[0], None

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
