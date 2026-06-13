from django.contrib import admin

from .models import MidasbuyAccount, MidasbuyLoginAttempt


@admin.register(MidasbuyAccount)
class MidasbuyAccountAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "label",
        "email",
        "status",
        "is_flagged",
        "consecutive_errors",
        "last_login_at",
        "updated_at",
    )
    list_filter = ("status", "is_flagged", "created_at", "updated_at")
    search_fields = ("label", "email", "phone")
    readonly_fields = ("created_at", "updated_at", "last_login", "last_login_at", "flagged_at")
    actions = ["clear_flag"]

    @admin.action(description="Clear flag (make available for rotation again)")
    def clear_flag(self, request, queryset):
        n = queryset.update(is_flagged=False, consecutive_errors=0, flagged_at=None)
        self.message_user(request, f"Cleared flags on {n} account(s).")
    exclude = (
        "password",
        "cookie_data",
        "storage_state_data",
        "session_path",
        "storage_state_path",
        "token_path",
    )


@admin.register(MidasbuyLoginAttempt)
class MidasbuyLoginAttemptAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "result", "created_at")
    list_filter = ("result", "created_at")
    search_fields = ("account__label", "account__email", "message")
    readonly_fields = (
        "account",
        "result",
        "message",
        "screenshot_path",
        "html_snapshot_path",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
