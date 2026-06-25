# ── Web image: FastAPI + Django ASGI + Chromium ────────────────────────────────
# Account login, player lookup and redeem all drive a (headless) browser inside
# this process, so the web image must carry Chromium too — same browser deps as
# Dockerfile.worker. Without it, p.chromium.launch() fails with
# "Executable doesn't exist at .../chromium-*/chrome".

# Stage 1 — build the virtualenv (wheels only; psycopg2-binary needs no compiler)
FROM python:3.12-slim AS builder
WORKDIR /app

RUN python -m pip install --upgrade pip && python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2 — runtime + Chromium
FROM python:3.12-slim
WORKDIR /app

# Shared libraries Chromium needs at runtime, plus xvfb/xauth for headful runs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdrm2 libxkbcommon0 \
    libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libgtk-3-0 \
    libasound2 libpango-1.0-0 libcairo2 fonts-liberation xvfb xauth tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Download ONLY Chromium for patchright (the project's stealth Playwright fork).
RUN python -m patchright install chromium

COPY . .

# Entrypoint starts a virtual X display (for the headful login browser), then
# execs the command below. Placed in /usr/local/bin so the `.:/app` dev mount
# can't shadow it.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# ASGI server hosting both Django (/) and FastAPI (/api).
CMD ["uvicorn", "midasbuy_project.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
