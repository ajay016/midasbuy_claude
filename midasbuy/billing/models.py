"""
Metered subscriptions.

Each client can hold up to two INDEPENDENT subscriptions — one per plan:

  * ``panel`` — requests made through the dashboard (JWT-authenticated API calls)
  * ``api``   — server-to-server requests (HMAC-signed API calls)

Both are billed the same way ($30 per 5000 requests / 30-day window by default)
but are entirely separate: a client may have one, both, or neither. Admins and
staff are internal and never metered.

Every billable request calls ``consume()``, which rolls the window over when it
has elapsed and increments usage atomically (``SELECT ... FOR UPDATE``) so two
concurrent requests can't both slip past the limit.
"""
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

PERIOD_DAYS = 30
DEFAULT_LIMIT = 5000
DEFAULT_PRICE_CENTS = 3000  # $30.00


class Subscription(models.Model):
    PLAN_PANEL = "panel"
    PLAN_API = "api"
    PLAN_CHOICES = [(PLAN_PANEL, "Panel"), (PLAN_API, "API")]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions"
    )
    plan = models.CharField(max_length=10, choices=PLAN_CHOICES)

    request_limit = models.PositiveIntegerField(default=DEFAULT_LIMIT)
    price_cents = models.PositiveIntegerField(default=DEFAULT_PRICE_CENTS)
    is_active = models.BooleanField(default=True)

    used = models.PositiveIntegerField(default=0)
    period_start = models.DateTimeField(default=timezone.now)
    period_end = models.DateTimeField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # One subscription row per (user, plan); renewing just resets it.
        unique_together = [("user", "plan")]
        ordering = ["plan"]

    def __str__(self):
        return f"{self.user_id}:{self.plan} {self.used}/{self.request_limit}"

    # ── Derived state ──────────────────────────────────────────────────────────
    @property
    def remaining(self) -> int:
        return max(0, self.request_limit - self.used)

    @property
    def used_pct(self) -> int:
        if not self.request_limit:
            return 0
        return min(100, round(self.used / self.request_limit * 100))

    @property
    def price_dollars(self) -> str:
        return f"{self.price_cents / 100:.2f}"

    @property
    def is_current(self) -> bool:
        return self.is_active and self.period_start <= timezone.now() < self.period_end

    def save(self, *args, **kwargs):
        if not self.period_end:
            self.period_end = (self.period_start or timezone.now()) + timedelta(days=PERIOD_DAYS)
        super().save(*args, **kwargs)

    # ── Metering ───────────────────────────────────────────────────────────────
    @classmethod
    def current_for(cls, user_id: int, plan: str):
        return cls.objects.filter(user_id=user_id, plan=plan, is_active=True).first()

    def consume(self, n: int = 1) -> bool:
        """Atomically roll the window if elapsed, then charge ``n`` units.

        Returns False (and charges nothing) when the limit would be exceeded.
        """
        now = timezone.now()
        with transaction.atomic():
            sub = Subscription.objects.select_for_update().get(pk=self.pk)
            if now >= sub.period_end:  # window elapsed -> fresh allowance
                sub.period_start = now
                sub.period_end = now + timedelta(days=PERIOD_DAYS)
                sub.used = 0
            if sub.used + n > sub.request_limit:
                sub.save(update_fields=["period_start", "period_end", "used", "updated_at"])
                return False
            sub.used += n
            sub.save(update_fields=["period_start", "period_end", "used", "updated_at"])
        # keep the in-memory instance roughly in sync for callers/templates
        self.period_start, self.period_end, self.used = sub.period_start, sub.period_end, sub.used
        return True

    @classmethod
    def grant(cls, user, plan: str, request_limit: int = DEFAULT_LIMIT,
              price_cents: int = DEFAULT_PRICE_CENTS):
        """Create or renew a subscription: activate, set the limit, reset the window."""
        now = timezone.now()
        sub, _ = cls.objects.update_or_create(
            user=user, plan=plan,
            defaults={
                "request_limit": request_limit,
                "price_cents": price_cents,
                "is_active": True,
                "used": 0,
                "period_start": now,
                "period_end": now + timedelta(days=PERIOD_DAYS),
            },
        )
        return sub

    @classmethod
    def summary_for(cls, user) -> dict:
        """{'panel': Subscription|None, 'api': Subscription|None} for display."""
        subs = {s.plan: s for s in cls.objects.filter(user=user)}
        return {cls.PLAN_PANEL: subs.get(cls.PLAN_PANEL), cls.PLAN_API: subs.get(cls.PLAN_API)}
