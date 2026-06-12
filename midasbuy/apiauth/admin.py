import secrets

from django import forms
from django.contrib import admin, messages

from .models import ApiKey, Merchant


class MerchantAdminForm(forms.ModelForm):
    """Admin form that lets you set a password when creating/editing a merchant."""
    new_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput,
        help_text="Set or replace the merchant's panel-login password.",
    )

    class Meta:
        model = Merchant
        fields = ["name", "email", "is_active"]

    def save(self, commit=True):
        merchant = super().save(commit=False)
        pwd = self.cleaned_data.get("new_password")
        if pwd:
            merchant.set_password(pwd)
        elif not merchant.password:
            # Created without a password: assign a random one (login disabled until reset).
            merchant.set_password(secrets.token_urlsafe(32))
        if commit:
            merchant.save()
        return merchant


class ApiKeyInline(admin.TabularInline):
    model = ApiKey
    extra = 0
    readonly_fields = ("key_id", "label", "is_active", "created_at", "last_used_at")
    fields = ("key_id", "label", "is_active", "created_at", "last_used_at")
    can_delete = True


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    form = MerchantAdminForm
    list_display = ("id", "name", "email", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "email")
    inlines = [ApiKeyInline]
    actions = ["generate_api_key"]

    @admin.action(description="Generate a new API key (secret shown once)")
    def generate_api_key(self, request, queryset):
        for merchant in queryset:
            key, secret = ApiKey.generate(merchant, label="admin-generated")
            self.message_user(
                request,
                f"{merchant.email} — key_id={key.key_id}  secret={secret}  "
                f"(copy the secret now; it is not stored in readable form)",
                level=messages.WARNING,
            )


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("key_id", "merchant", "label", "is_active", "created_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("key_id", "merchant__email")
    readonly_fields = ("key_id", "secret_encrypted", "created_at", "last_used_at")
