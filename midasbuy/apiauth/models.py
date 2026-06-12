"""
Merchants and their API credentials.

A Merchant is a customer of this API. They authenticate two ways, both resolving
to the same Merchant:

  * Dashboard / browser  -> email + password -> short-lived JWT
  * Server-to-server      -> ApiKey (key id + secret) -> HMAC-signed requests

Passwords are hashed with Django's password hashers (slow, salted) — we never
need them back. API secrets are different: HMAC verification must recompute the
signature, so the secret has to be recoverable. We therefore store it ENCRYPTED
(Fernet, key held outside the DB) instead of hashed — a DB-only leak yields
nothing usable.
"""
import secrets

from django.contrib.auth.hashers import check_password, make_password
from django.db import models

from .security import decrypt_secret, encrypt_secret


class Merchant(models.Model):
    """A panel user / API customer.

    One table backs three kinds of people, distinguished by ``role``:

      * ``admin``  — full access: manages users, bot accounts, API keys, and can
                     order. Every capability flag is implied True regardless of
                     its stored value.
      * ``staff``  — operates the panel. May order ONLY if granted ``can_order``;
                     never manages users.
      * ``client`` — a customer. Limited menu; orders if allowed, and gets API
                     access only when ``can_use_api`` is on (their subscription).

    Capability flags are checked through the ``allowed_to_*`` helpers below, which
    fold in the "admin implies everything" rule — always gate on those, never on
    the raw boolean, so a new admin is never accidentally locked out.
    """

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
    password = models.CharField(max_length=255)  # hashed, never plaintext
    is_active = models.BooleanField(default=True)

    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_CLIENT)

    # Granular capabilities (ignored for admins, who get everything).
    can_order = models.BooleanField(
        default=False, help_text="May look up players and redeem (panel + API)."
    )
    can_manage_accounts = models.BooleanField(
        default=False, help_text="May manage the upstream Midasbuy bot accounts."
    )
    can_use_api = models.BooleanField(
        default=False, help_text="May create API keys and call the API directly."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} <{self.email}>"

    def set_password(self, raw_password: str) -> None:
        self.password = make_password(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password(raw_password, self.password)

    # ── Role helpers ───────────────────────────────────────────────────────────
    @property
    def is_admin(self) -> bool:
        return self.role == self.ROLE_ADMIN

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
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="api_keys")
    key_id = models.CharField(max_length=48, unique=True, db_index=True)  # public id
    secret_encrypted = models.TextField()                                  # Fernet token
    label = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.key_id} ({self.merchant.email})"

    @classmethod
    def generate(cls, merchant: "Merchant", label: str = "") -> tuple["ApiKey", str]:
        """Create a key and return (instance, plaintext_secret). Secret shown ONCE."""
        key_id = "mk_" + secrets.token_hex(16)
        secret = "sk_" + secrets.token_urlsafe(32)
        obj = cls.objects.create(
            merchant=merchant,
            key_id=key_id,
            secret_encrypted=encrypt_secret(secret),
            label=label,
        )
        return obj, secret

    def get_secret(self) -> str:
        """Decrypt and return the plaintext secret (server-side use only)."""
        return decrypt_secret(self.secret_encrypted)
