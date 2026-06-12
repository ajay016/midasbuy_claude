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

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

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
