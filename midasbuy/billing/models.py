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

PLAN_PANEL = "panel"
PLAN_API = "api"
PLAN_CHOICES = [(PLAN_PANEL, "Panel"), (PLAN_API, "API")]


class Package(models.Model):
    """A reusable subscription package an admin can define once and assign to many
    clients, instead of typing a raw request limit each time.

    ``request_limit = NULL`` means *unlimited* (used for partner/internal plans that
    are never capped but whose usage we still want to meter)."""

    name = models.CharField(max_length=80)
    plan = models.CharField(
        max_length=10, choices=PLAN_CHOICES,
        help_text="Which meter this package applies to (panel or API).",
    )
    request_limit = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Requests allowed per period. Leave blank for UNLIMITED.",
    )
    price_cents = models.PositiveIntegerField(default=DEFAULT_PRICE_CENTS)
    period_days = models.PositiveIntegerField(default=PERIOD_DAYS)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["plan", "price_cents", "name"]

    def __str__(self):
        cap = "unlimited" if self.request_limit is None else f"{self.request_limit}"
        return f"{self.name} ({self.plan}, {cap}/{self.period_days}d)"

    @property
    def is_unlimited(self) -> bool:
        return self.request_limit is None

    @property
    def price_dollars(self) -> str:
        return f"{self.price_cents / 100:.2f}"


class Subscription(models.Model):
    PLAN_PANEL = PLAN_PANEL
    PLAN_API = PLAN_API
    PLAN_CHOICES = PLAN_CHOICES

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions"
    )
    plan = models.CharField(max_length=10, choices=PLAN_CHOICES)

    # NULL request_limit = unlimited (admins/partners): never rejected, still metered.
    request_limit = models.PositiveIntegerField(
        null=True, blank=True, default=DEFAULT_LIMIT,
        help_text="Requests per period. NULL = unlimited (still counted).",
    )
    price_cents = models.PositiveIntegerField(default=DEFAULT_PRICE_CENTS)
    period_days = models.PositiveIntegerField(default=PERIOD_DAYS)
    is_active = models.BooleanField(default=True)

    # Optional link to the catalog package this subscription was granted from.
    package = models.ForeignKey(
        Package, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="subscriptions",
    )

    used = models.PositiveIntegerField(default=0)
    period_start = models.DateTimeField(default=timezone.now)
    period_end = models.DateTimeField()

    # ── Placeholders for features we'll implement later (kept so the schema and
    # admin are ready and don't need another migration when we wire them up) ──
    # Auto-renew: when True, a future scheduled task will roll the window AND
    # trigger a charge at period_end instead of just resetting the meter lazily.
    auto_renew = models.BooleanField(
        default=False, help_text="Reserved: auto-renew + bill at period end (not active yet)."
    )
    # Real payment processing: reference to the gateway's record (Stripe/etc.).
    payment_ref = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Reserved: external payment/gateway reference (not active yet)."
    )

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
    def is_unlimited(self) -> bool:
        return self.request_limit is None

    @property
    def remaining(self):
        """Remaining requests, or None when unlimited."""
        if self.request_limit is None:
            return None
        return max(0, self.request_limit - self.used)

    @property
    def limit_label(self) -> str:
        return "∞" if self.request_limit is None else str(self.request_limit)

    @property
    def used_pct(self) -> int:
        # Unlimited plans have no bar to fill.
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
            self.period_end = (self.period_start or timezone.now()) + timedelta(
                days=self.period_days or PERIOD_DAYS
            )
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
                sub.period_end = now + timedelta(days=sub.period_days or PERIOD_DAYS)
                sub.used = 0
            # request_limit is None -> unlimited: always allow, but still count usage.
            if sub.request_limit is not None and sub.used + n > sub.request_limit:
                sub.save(update_fields=["period_start", "period_end", "used", "updated_at"])
                return False
            sub.used += n
            sub.save(update_fields=["period_start", "period_end", "used", "updated_at"])
        # keep the in-memory instance roughly in sync for callers/templates
        self.period_start, self.period_end, self.used = sub.period_start, sub.period_end, sub.used
        return True

    @classmethod
    def grant(cls, user, plan: str, request_limit=DEFAULT_LIMIT,
              price_cents: int = DEFAULT_PRICE_CENTS, period_days: int = PERIOD_DAYS,
              package=None):
        """Create or renew a subscription: activate, set the limit, reset the window.

        ``request_limit=None`` grants an UNLIMITED plan (still metered)."""
        now = timezone.now()
        sub, _ = cls.objects.update_or_create(
            user=user, plan=plan,
            defaults={
                "request_limit": request_limit,
                "price_cents": price_cents,
                "period_days": period_days or PERIOD_DAYS,
                "package": package,
                "is_active": True,
                "used": 0,
                "period_start": now,
                "period_end": now + timedelta(days=period_days or PERIOD_DAYS),
            },
        )
        return sub

    @classmethod
    def grant_package(cls, user, package: "Package"):
        """Grant/renew the subscription described by a catalog ``Package``."""
        return cls.grant(
            user, package.plan,
            request_limit=package.request_limit,
            price_cents=package.price_cents,
            period_days=package.period_days,
            package=package,
        )

    @classmethod
    def summary_for(cls, user) -> dict:
        """{'panel': Subscription|None, 'api': Subscription|None} for display."""
        subs = {s.plan: s for s in cls.objects.filter(user=user)}
        return {cls.PLAN_PANEL: subs.get(cls.PLAN_PANEL), cls.PLAN_API: subs.get(cls.PLAN_API)}
