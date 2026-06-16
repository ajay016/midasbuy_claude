# ── Web image: FastAPI + Django ASGI, NO browser ───────────────────────────────
# This container only serves the API and enqueues Celery jobs, so it stays small
# (~200 MB). All Playwright/Chromium weight lives in Dockerfile.worker instead.

# Stage 1 — build the virtualenv (wheels only; psycopg2-binary needs no compiler)
FROM python:3.12-slim AS builder
WORKDIR /app

RUN python -m pip install --upgrade pip && python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2 — runtime
FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY . .

EXPOSE 8000
# ASGI server hosting both Django (/) and FastAPI (/api)
CMD ["uvicorn", "midasbuy_project.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
