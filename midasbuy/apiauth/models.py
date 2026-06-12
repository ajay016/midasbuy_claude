"""
The project's custom user model and API credentials.

`User` REPLACES Django's default `auth.User` (via `AUTH_USER_MODEL = "apiauth.User"`).
It is the single identity for everything:

  * Panel / browser  -> Django session auth -> `request.user` IS a User
  * Server-to-server -> ApiKey (key id + secret) -> HMAC-signed requests

Three kinds of people share the table, distinguished by `role` (admin/staff/client)
plus granular capability flags. Always gate on the `allowed_to_*` helpers, which
fold in "admin (or Django superuser) implies everything".

Passwords are hashed (AbstractBaseUser). API secrets must be recoverable to verify
HMAC signatures, so they're stored ENCRYPTED (Fernet, key outside the DB), not hashed.
"""
import secrets

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models

from .security import decrypt_secret, encrypt_secret


class UserManager(BaseUserManager):
    """Manager so `createsuperuser` and programmatic creation work with email login."""

    use_in_migrations = True

    def _create(self, email, password, **extra):
        if not email:
            raise ValueError("Users must have an email address.")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("role", User.ROLE_CLIENT)
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("role", User.ROLE_ADMIN)
        extra["is_staff"] = True
        extra["is_superuser"] = True
        extra["is_active"] = True
        return self._create(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    ROLE_ADMIN = "admin"
    ROLE_STAFF = "staff"
    ROLE_CLIENT = "client"
    ROLE_CHOICES = [
        (ROLE_ADMIN, "Admin"),
        (ROLE_STAFF, "Staff"),
        (ROLE_CLIENT, "Client"),
    ]

    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)

    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_CLIENT)

    # Granular capabilities (ignored for admins/superusers, who get everything).
    can_order = models.BooleanField(
        default=False, help_text="May look up players and redeem (panel + API)."
    )
    can_manage_accounts = models.BooleanField(
        default=False, help_text="May manage the upstream Midasbuy bot accounts."
    )
    can_use_api = models.BooleanField(
        default=False, help_text="May create API keys and call the API directly."
    )

    # Required by Django's auth / admin.
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False, help_text="Can log into the Django admin site."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]  # prompted by createsuperuser, besides email + password

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} <{self.email}>"

    # ── Role helpers ───────────────────────────────────────────────────────────
    @property
    def is_admin(self) -> bool:
        return self.role == self.ROLE_ADMIN or self.is_superuser

    @property
    def role_label(self) -> str:
        return dict(self.ROLE_CHOICES).get(self.role, self.role)

    @property
    def allowed_to_order(self) -> bool:
        return self.is_admin or self.can_order

    @property
    def allowed_to_manage_accounts(self) -> bool:
        return self.is_admin or self.can_manage_accounts

    @property
    def allowed_to_use_api(self) -> bool:
        return self.is_admin or self.can_use_api

    @property
    def allowed_to_manage_users(self) -> bool:
        # Only admins ever manage other users.
        return self.is_admin

    def capabilities(self) -> dict:
        """Flat snapshot used by the API auth layer and templates."""
        return {
            "role": self.role,
            "is_admin": self.is_admin,
            "can_order": self.allowed_to_order,
            "can_manage_accounts": self.allowed_to_manage_accounts,
            "can_use_api": self.allowed_to_use_api,
            "can_manage_users": self.allowed_to_manage_users,
        }


class ApiKey(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    key_id = models.CharField(max_length=48, unique=True, db_index=True)  # public id
    secret_encrypted = models.TextField()                                  # Fernet token
    label = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.key_id} ({self.user.email})"

    @classmethod
    def generate(cls, user: "User", label: str = "") -> tuple["ApiKey", str]:
        """Create a key and return (instance, plaintext_secret). Secret shown ONCE."""
        key_id = "mk_" + secrets.token_hex(16)
        secret = "sk_" + secrets.token_urlsafe(32)
        obj = cls.objects.create(
            user=user,
            key_id=key_id,
            secret_encrypted=encrypt_secret(secret),
            label=label,
        )
        return obj, secret

    def get_secret(self) -> str:
        """Decrypt and return the plaintext secret (server-side use only)."""
        return decrypt_secret(self.secret_encrypted)
