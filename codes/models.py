"""
Code vault + redemption history. See docs/CODE_VAULT.md.

Codes are value-bearing, so the full code is only stored ENCRYPTED (Fernet, key
outside the DB). Lookups use an HMAC fingerprint so we never need to decrypt to
find a code, and the UI shows only the last 4 characters.
"""
from django.conf import settings
from django.db import models


class CodeStatus(models.TextChoices):
    UNCHECKED = "unchecked", "Unchecked"
    VALID = "valid", "Valid"
    USED = "used", "Used"
    EXPIRED = "expired", "Expired"
    INVALID = "invalid", "Invalid"
    REDEEMING = "redeeming", "Redeeming"


class StoredCode(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="stored_codes")
    code_encrypted = models.TextField()
    fingerprint = models.CharField(max_length=64, db_index=True)
    code_last4 = models.CharField(max_length=8)
    label = models.CharField(max_length=120, blank=True, default="")
    country_code = models.CharField(max_length=8, default="bd")

    status = models.CharField(max_length=12, choices=CodeStatus.choices,
                              default=CodeStatus.UNCHECKED, db_index=True)
    product_name = models.CharField(max_length=255, blank=True, default="")
    checked_at = models.DateTimeField(null=True, blank=True)

    redeemed_player_id = models.CharField(max_length=64, blank=True, default="")
    redeemed_username = models.CharField(max_length=128, blank=True, default="")
    redeemed_at = models.DateTimeField(null=True, blank=True)
    redeemed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "fingerprint"],
                                    name="uniq_stored_code_per_owner"),
        ]

    def __str__(self):
        return f"••••{self.code_last4} ({self.status}) owner={self.owner_id}"

    def get_code(self) -> str:
        from apiauth.security import decrypt_secret

        return decrypt_secret(self.code_encrypted)


class EventKind(models.TextChoices):
    ADDED = "added", "Added"
    CHECKED = "checked", "Checked"
    REDEEMED = "redeemed", "Redeemed"
    REDEEM_FAILED = "redeem_failed", "Redeem failed"
    DELETED = "deleted", "Deleted"


class CodeEvent(models.Model):
    """Append-only history. Survives deletion of the stored code, and also records
    redeems of codes that were never stored (so "used on which player?" works for
    every code our app redeemed)."""
    fingerprint = models.CharField(max_length=64, db_index=True)
    code_last4 = models.CharField(max_length=8)
    stored_code = models.ForeignKey(StoredCode, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="events")
    kind = models.CharField(max_length=16, choices=EventKind.choices, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="+")
    source = models.CharField(max_length=16, blank=True, default="")
    player_id = models.CharField(max_length=64, blank=True, default="")
    username = models.CharField(max_length=128, blank=True, default="")
    product_name = models.CharField(max_length=255, blank=True, default="")
    order_no = models.CharField(max_length=128, blank=True, default="")
    result_status = models.CharField(max_length=20, blank=True, default="")
    message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.kind} ••••{self.code_last4} {self.player_id}".strip()
