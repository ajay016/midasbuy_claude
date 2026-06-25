from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Environment (.env) ─────────────────────────────────────────────────────────
# django-environ gives typed access (env.bool / env.int / env.list) AND loads the
# values into os.environ, so existing os.getenv() calls (captcha config) keep
# working. read_env is a no-op if the file is missing (e.g. in CI/containers that
# inject real env vars instead of a file).
env = environ.Env(
    DEBUG=(bool, True),
    PRODUCTION=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env(
    "SECRET_KEY",
    default="django-insecure-change-this-in-production-use-env-var",
)

DEBUG = env.bool("DEBUG", default=True)

# Comma-separated in .env, e.g. ALLOWED_HOSTS=127.0.0.1,localhost,1.2.3.4
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "redeem",
    "bulk",
    "apiauth",
    "billing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "midasbuy_project.urls"

# Our own model replaces Django's default auth.User — it's the single identity for
# the panel (session auth), the API (JWT/HMAC), and the admin site.
AUTH_USER_MODEL = "apiauth.User"
# Where @login_required and the panel decorators send anonymous users.
LOGIN_URL = "panel_login"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csrf",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ASGI_APPLICATION = "midasbuy_project.asgi.application"

# ── Database ───────────────────────────────────────────────────────────────────
# PostgreSQL by default, reading credentials from .env. The DB runs on the HOST
# (your Windows machine), NOT in a container — DB_HOST=host.docker.internal lets
# the containers reach the host's Postgres. Set DB_ENGINE=sqlite to fall back to
# the bundled SQLite file (handy for a quick local run without Postgres).
if env("DB_ENGINE", default="postgresql") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME", default="midasbuy"),
            "USER": env("DB_USER", default="postgres"),
            "PASSWORD": env("DB_PASSWORD", default="postgres"),
            "HOST": env("DB_HOST", default="127.0.0.1"),
            "PORT": env("DB_PORT", default="5432"),
        }
    }

# ── Celery / Redis ─────────────────────────────────────────────────────────────
# Redis is both the broker (queue) and the result backend. Inside docker-compose
# the hostname is the `redis` service; locally (no docker) it's 127.0.0.1.
CELERY_BROKER_URL = env("REDIS_URL", default="redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = env("REDIS_URL", default="redis://127.0.0.1:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
# Browser redeem tasks are long-running; give them room and avoid silent loss.
CELERY_TASK_ACKS_LATE = True                 # re-queue if a worker dies mid-task
CELERY_WORKER_PREFETCH_MULTIPLIER = 1        # one heavy task per worker slot
CELERY_TASK_TRACK_STARTED = True
CELERY_RESULT_EXTENDED = True

# ── API authentication ─────────────────────────────────────────────────────────
# JWT signing secret for dashboard/browser tokens. Defaults to SECRET_KEY; set a
# dedicated value in production.
JWT_SECRET = env("JWT_SECRET", default=SECRET_KEY)
# Fernet key used to encrypt API secrets at rest (so a DB leak is not enough to
# forge requests). Generate one with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# If unset, a stable key is derived from SECRET_KEY (fine for dev, set it in prod).
APIAUTH_FERNET_KEY = env("APIAUTH_FERNET_KEY", default="")
# Secret used to sign outgoing webhook payloads (X-Webhook-Signature). Defaults to
# JWT_SECRET. Receivers verify HMAC-SHA256(WEBHOOK_SECRET, raw_body).
WEBHOOK_SECRET = env("WEBHOOK_SECRET", default=JWT_SECRET)
# Users are created by an admin (panel or Django admin), so self-registration is OFF
# by default. Set to True only if you want an open POST /api/auth/register.
APIAUTH_OPEN_REGISTRATION = env.bool("APIAUTH_OPEN_REGISTRATION", default=False)

# ── Static & media ──────────────────────────────────────────────────────────────
# Source static lives inside each app's `static/` folder (AppDirectoriesFinder
# picks it up automatically) — no project-level STATICFILES_DIRS. `collectstatic`
# gathers everything into STATIC_ROOT for the web server to serve in production.
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"   # generated by collectstatic — gitignored

# User-uploaded files.
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"          # runtime uploads — gitignored

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[%(levelname)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "accounts": {"level": "DEBUG", "handlers": ["console"], "propagate": False},
        "api": {"level": "DEBUG", "handlers": ["console"], "propagate": False},
        "bulk": {"level": "INFO", "handlers": ["console"], "propagate": False},
    },
}

# Midasbuy browser settings
MIDASBUY_BROWSER_HEADLESS = env.bool("MIDASBUY_BROWSER_HEADLESS", default=False)
MIDASBUY_CRYPTO_BROWSER_HEADLESS = env.bool("MIDASBUY_CRYPTO_BROWSER_HEADLESS", default=True)
MIDASBUY_BROWSER_USER_AGENT = env("MIDASBUY_BROWSER_USER_AGENT", default="")
MIDASBUY_BROWSER_VIEWPORT = {"width": 1440, "height": 900}
MIDASBUY_LOGIN_BASE_URL = "https://www.midasbuy.com/midasbuy"

# Account rotation: per-account cap (redeem/code-status calls per minute) and how
# repeated upstream errors flag an account out of rotation.
MIDASBUY_ACCOUNT_CAP_PER_MIN = env.int("MIDASBUY_ACCOUNT_CAP_PER_MIN", default=20)
MIDASBUY_ACCOUNT_FLAG_THRESHOLD = env.int("MIDASBUY_ACCOUNT_FLAG_THRESHOLD", default=5)
MIDASBUY_ACCOUNT_FLAG_COOLDOWN = env.int("MIDASBUY_ACCOUNT_FLAG_COOLDOWN", default=600)

# Browser warm-up: on API startup, pre-build a cached browser session for every
# logged-in account (per country below) so the first real request to each account
# isn't a slow cold start. A periodic keep-warm refreshes them so rotation stays
# fast. Set MIDASBUY_WARM_ON_STARTUP=False to disable. Each warm account = one
# headless Chromium per worker, so keep gunicorn workers low (1-2) when warming.
# Trust the left-most X-Forwarded-For entry as the client IP for API IP allow-lists.
# Keep True when behind nginx/a load balancer (the usual deploy); set False only if
# the app is exposed directly to clients with no proxy.
MIDASBUY_TRUST_FORWARDED_FOR = env.bool("MIDASBUY_TRUST_FORWARDED_FOR", default=True)

MIDASBUY_WARM_ON_STARTUP = env.bool("MIDASBUY_WARM_ON_STARTUP", default=True)
MIDASBUY_WARM_COUNTRIES = env.list("MIDASBUY_WARM_COUNTRIES", default=["bd"])
# Seconds between background keep-warm sweeps. 0 disables it (sessions then go stale
# and the NEXT request pays a cold rebuild — the "occasional slow lookup"). A few
# minutes keeps every account's cached browser session hot so real requests stay fast.
MIDASBUY_WARM_INTERVAL = env.int("MIDASBUY_WARM_INTERVAL", default=300)
# Seconds between keep-warm sweeps (0 disables the periodic refresh; startup warm
# still runs). Already-warm sessions are reused cheaply; only expired ones rebuild.
MIDASBUY_WARM_INTERVAL = env.int("MIDASBUY_WARM_INTERVAL", default=600)
