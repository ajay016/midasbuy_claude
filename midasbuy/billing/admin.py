from django.contrib import admin

from .models import Subscription


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "used", "request_limit", "is_active",
                    "period_end", "price_cents")
    list_filter = ("plan", "is_active")
    search_fields = ("user__email", "user__name")
    readonly_fields = ("used", "period_start", "period_end", "created_at", "updated_at")
