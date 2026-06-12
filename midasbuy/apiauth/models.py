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
    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=255)  # hashed, never plaintext
    is_active = models.BooleanField(default=True)

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
