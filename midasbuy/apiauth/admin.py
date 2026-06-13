from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserCreationForm

from .models import ApiKey, User


class UserCreateForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("email", "name")


class ApiKeyInline(admin.TabularInline):
    model = ApiKey
    extra = 0
    readonly_fields = ("key_id", "label", "is_active", "created_at", "last_used_at")
    fields = ("key_id", "label", "is_active", "created_at", "last_used_at")
    can_delete = True


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    add_form = UserCreateForm
    model = User
    ordering = ("-created_at",)
    list_display = ("id", "name", "email", "role", "is_active", "created_at")
    list_filter = ("role", "is_active", "can_order", "can_use_api")
    search_fields = ("name", "email")
    readonly_fields = ("created_at", "updated_at", "last_login")
    inlines = [ApiKeyInline]
    actions = ["generate_api_key"]

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("name",)}),
        ("Role & capabilities", {
            "fields": ("role", "can_order", "can_manage_accounts", "can_use_api",
                       "rate_limit_per_min"),
        }),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser",
                                    "groups", "user_permissions")}),
        ("Timestamps", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "name", "role", "password1", "password2"),
        }),
    )

    @admin.action(description="Generate a new API key (secret shown once)")
    def generate_api_key(self, request, queryset):
        for user in queryset:
            key, secret = ApiKey.generate(user, label="admin-generated")
            self.message_user(
                request,
                f"{user.email} — key_id={key.key_id}  secret={secret}  "
                f"(copy the secret now; it is not stored in readable form)",
                level=messages.WARNING,
            )


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("key_id", "user", "label", "is_active", "created_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("key_id", "user__email")
    readonly_fields = ("key_id", "secret_encrypted", "created_at", "last_used_at")
