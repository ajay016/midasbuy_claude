from django.contrib import admin

from .models import CodeEvent, StoredCode


@admin.register(StoredCode)
class StoredCodeAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "code_last4", "status", "product_name",
                    "redeemed_player_id", "redeemed_at", "created_at")
    list_filter = ("status",)
    search_fields = ("code_last4", "label", "redeemed_player_id", "owner__email")
    exclude = ("code_encrypted",)
    readonly_fields = ("fingerprint",)


@admin.register(CodeEvent)
class CodeEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "code_last4", "actor", "source", "player_id",
                    "result_status")
    list_filter = ("kind", "source")
    search_fields = ("code_last4", "player_id", "order_no")
