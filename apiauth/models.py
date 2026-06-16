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
import ipaddress
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
    ROLE_PARTNER = "partner"
    ROLE_CHOICES = [
        (ROLE_ADMIN, "Admin"),
        (ROLE_STAFF, "Staff"),
        (ROLE_CLIENT, "Client"),
        (ROLE_PARTNER, "Partner"),
    ]

    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)

    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_CLIENT)

    # ── Partner / reseller model ────────────────────────────────────────────────
    # A *partner* resells our API to their own end-customers. Each of those
    # customers is a Client row whose ``partner`` points back at the partner that
    # owns them. The partner manages only their own clients (panel + API).
    partner = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="clients", limit_choices_to={"role": ROLE_PARTNER},
        help_text="If set, the partner that owns this client.",
    )
    # Opaque per-client identifier. Because every request from a partner's website
    # arrives with the SAME (partner's) source IP, the partner passes this value
    # (header X-Client-Id) so we can attribute the request to the right client.
    client_ref = models.CharField(
        max_length=40, unique=True, blank=True, db_index=True,
        help_text="Identifier the partner sends (X-Client-Id) to select this client.",
    )
    # Optional IP allow-list (comma/newline separated IPs or CIDR ranges). When set,
    # signed API requests are only accepted from these addresses. Empty = no IP check.
    allowed_ips = models.TextField(
        blank=True, default="",
        help_text="Optional allow-list of IPs/CIDRs (comma or newline separated). "
                  "Empty = allow any source IP.",
    )

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

    # Max billable API/panel requests per minute (0 = unlimited). Admin-tunable.
    rate_limit_per_min = models.PositiveIntegerField(
        default=20, help_text="Requests per minute before HTTP 429 (0 = unlimited)."
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

    def save(self, *args, **kwargs):
        # Every user gets a stable, opaque client identifier the first time it's
        # saved. Partners pass their clients' refs (X-Client-Id) on API calls.
        if not self.client_ref:
            self.client_ref = self._generate_client_ref()
        super().save(*args, **kwargs)

    @classmethod
    def _generate_client_ref(cls) -> str:
        for _ in range(10):
            ref = "cl_" + secrets.token_hex(8)
            if not cls.objects.filter(client_ref=ref).exists():
                return ref
        return "cl_" + secrets.token_hex(16)

    # ── Role helpers ───────────────────────────────────────────────────────────
    @property
    def is_admin(self) -> bool:
        return self.role == self.ROLE_ADMIN or self.is_superuser

    @property
    def is_partner(self) -> bool:
        return self.role == self.ROLE_PARTNER

    # ── IP allow-list ──────────────────────────────────────────────────────────
    def ip_allowed(self, ip: str) -> bool:
        """True if ``ip`` is permitted. No allow-list configured -> any IP allowed."""
        entries = [e.strip() for e in self.allowed_ips.replace("\n", ",").split(",")]
        entries = [e for e in entries if e]
        if not entries:
            return True
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        for entry in entries:
            try:
                if "/" in entry:
                    if addr in ipaddress.ip_network(entry, strict=False):
                        return True
                elif addr == ipaddress.ip_address(entry):
                    return True
            except ValueError:
                continue
        return False

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
        # Only admins ever manage the full user/team table.
        return self.is_admin

    @property
    def allowed_to_manage_clients(self) -> bool:
        # Admins manage everyone; partners manage their own clients.
        return self.is_admin or self.is_partner

    def owns_client(self, other: "User") -> bool:
        """Whether this user may manage ``other`` as one of their clients."""
        if self.is_admin:
            return True
        return self.is_partner and other.partner_id == self.id

    def capabilities(self) -> dict:
        """Flat snapshot used by the API auth layer and templates."""
        return {
            "role": self.role,
            "is_admin": self.is_admin,
            "is_partner": self.is_partner,
            "partner_id": self.partner_id,
            "client_ref": self.client_ref,
            "allowed_ips": self.allowed_ips,
            "can_order": self.allowed_to_order,
            "can_manage_accounts": self.allowed_to_manage_accounts,
            "can_use_api": self.allowed_to_use_api,
            "can_manage_users": self.allowed_to_manage_users,
            "can_manage_clients": self.allowed_to_manage_clients,
            "rate_limit_per_min": self.rate_limit_per_min,
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
