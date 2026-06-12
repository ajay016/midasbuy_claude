"""
Celery application for the Midasbuy project.

Celery is the background task system. The FastAPI/Django web process stays fast
by only *enqueueing* work; the heavy browser redeem jobs run in separate Celery
worker processes (the `worker` container), so a bulk request returns immediately
with a job id the client can poll.

Start a worker (from the directory containing manage.py):
    celery -A midasbuy_project worker -l info
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "midasbuy_project.settings")

app = Celery("midasbuy")

# Pull all CELERY_* keys from Django settings (broker, backend, serializers …).
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks.py in every installed app (e.g. bulk/tasks.py).
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
