from django.contrib import admin

from .models import BulkJob, BulkJobItem


class BulkJobItemInline(admin.TabularInline):
    model = BulkJobItem
    extra = 0
    readonly_fields = (
        "player_id", "pin_code", "zone_id", "status", "success",
        "username", "product_name", "message", "processed_at",
    )
    can_delete = False
    show_change_link = True


@admin.register(BulkJob)
class BulkJobAdmin(admin.ModelAdmin):
    list_display = (
        "id", "job_type", "status", "account", "country_code",
        "total_items", "succeeded_items", "failed_items", "created_at",
    )
    list_filter = ("job_type", "status", "country_code")
    search_fields = ("id", "celery_task_id")
    readonly_fields = ("celery_task_id", "created_at", "started_at", "finished_at")
    inlines = [BulkJobItemInline]


@admin.register(BulkJobItem)
class BulkJobItemAdmin(admin.ModelAdmin):
    list_display = (
        "id", "job", "player_id", "status", "success", "product_name", "processed_at",
    )
    list_filter = ("status", "success")
    search_fields = ("player_id", "pin_code")
