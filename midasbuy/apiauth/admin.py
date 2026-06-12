from django.contrib import admin

from .models import ApiKey, Merchant


class ApiKeyInline(admin.TabularInline):
    model = ApiKey
    extra = 0
    readonly_fields = ("key_id", "label", "is_active", "created_at", "last_used_at")
    fields = ("key_id", "label", "is_active", "created_at", "last_used_at")
    can_delete = True


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "email", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "email")
    exclude = ("password",)  # never expose the hash in the form
    inlines = [ApiKeyInline]


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("key_id", "merchant", "label", "is_active", "created_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("key_id", "merchant__email")
    readonly_fields = ("key_id", "secret_encrypted", "created_at", "last_used_at")
