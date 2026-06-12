"""
Persistence for bulk operations.

A BulkJob is one submitted batch (look up many players, validate many codes, or
redeem many codes). Each row to process is a BulkJobItem. The Celery worker
updates these rows as it goes, so the API can report live progress and a full
per-item result without keeping anything in memory.
"""
from django.db import models


class JobType(models.TextChoices):
    PLAYER_INFO = "player_info", "Player info lookup"
    VALIDATE = "validate", "Code validation"
    REDEEM = "redeem", "Redeem"


class JobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class ItemStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"


class BulkJob(models.Model):
    job_type = models.CharField(max_length=20, choices=JobType.choices)
    status = models.CharField(
        max_length=20, choices=JobStatus.choices, default=JobStatus.PENDING
    )

    # Which logged-in Midasbuy account performs the work, and for which storefront.
    account = models.ForeignKey(
        "accounts.MidasbuyAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bulk_jobs",
    )
    country_code = models.CharField(max_length=8, default="bd")

    # Progress counters (denormalised so the status endpoint is a single row read).
    total_items = models.PositiveIntegerField(default=0)
    processed_items = models.PositiveIntegerField(default=0)
    succeeded_items = models.PositiveIntegerField(default=0)
    failed_items = models.PositiveIntegerField(default=0)

    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"BulkJob #{self.pk} {self.job_type} ({self.status})"

    @property
    def progress_percent(self) -> int:
        if not self.total_items:
            return 0
        return round(self.processed_items * 100 / self.total_items)


class BulkJobItem(models.Model):
    job = models.ForeignKey(BulkJob, on_delete=models.CASCADE, related_name="items")

    # Inputs
    player_id = models.CharField(max_length=64)
    pin_code = models.CharField(max_length=64, blank=True, default="")
    zone_id = models.CharField(max_length=16, blank=True, default="1")

    # Outcome
    status = models.CharField(
        max_length=20, choices=ItemStatus.choices, default=ItemStatus.PENDING
    )
    success = models.BooleanField(default=False)
    message = models.TextField(blank=True, default="")

    # Useful extracted fields (avoid digging through `result` for the common cases)
    username = models.CharField(max_length=128, blank=True, default="")
    product_name = models.CharField(max_length=255, blank=True, default="")

    result = models.JSONField(default=dict, blank=True)  # full raw response

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"Item #{self.pk} {self.player_id} ({self.status})"
