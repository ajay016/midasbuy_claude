from django.contrib import admin

from .models import Package, Subscription


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ("name", "plan", "request_limit", "price_cents",
                    "period_days", "is_active")
    list_filter = ("plan", "is_active")
    search_fields = ("name",)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "used", "request_limit", "is_active",
                    "period_end", "price_cents", "package")
    list_filter = ("plan", "is_active")
    search_fields = ("user__email", "user__name")
    autocomplete_fields = ("user", "package")
    readonly_fields = ("used", "period_start", "period_end", "created_at", "updated_at")
