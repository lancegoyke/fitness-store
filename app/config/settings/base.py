import os
import sys
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

if "pytest" not in sys.modules:
    load_dotenv()


# Utilities
# ------------------------------------------------------------------------------
def get_env_var(var_name, default=None):
    """Get the environment variable or return exception."""
    try:
        return os.environ[var_name]
    except KeyError:
        if default is not None:
            return default
        error_msg = "Set the {} environment variable".format(var_name)
        raise Exception(error_msg)


# Custom Project settings
# ------------------------------------------------------------------------------
ENVIRONMENT = os.environ.get("ENVIRONMENT", "PRODUCTION")
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
PRODUCT_NAME_MAX_LENGTH = 80
DOMAIN_URL = os.environ.get("DOMAIN_URL", "http://localhost:8000/")
ADMINS = [
    ("Lance Goyke", "lance@lancegoyke.com"),
]
DEFAULT_FROM_EMAIL = "Lance Goyke <lance@lancegoyke.com>"
MANAGERS = ADMINS
SERVER_EMAIL = "Mastering Fitness <robot@mastering.fitness>"

# Build paths inside the project like this: BASE_DIR / 'subdir'.
ROOT_DIR = Path(__file__).resolve(strict=True).parent.parent.parent.parent
APP_DIR = ROOT_DIR / "app"
PROJECT_DIR = APP_DIR / "store_project"


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/3.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-klanmxuengq839ng")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = int(os.environ.get("DEBUG", default=0))

CORS_ALLOWED_ORIGINS = list(
    os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:8000 http://127.0.0.1:8000",
    ).split(" ")
)

ATOMIC_REQUESTS = True

ALLOWED_HOSTS = os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "localhost 127.0.0.1 [::1]"
).split(" ")

# Required by Django for HTTPS POST/admin behind a TLS-terminating proxy.
# Space-separated, scheme-qualified, e.g. "https://mastering.fitness https://www.mastering.fitness".
CSRF_TRUSTED_ORIGINS = [
    origin for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(" ") if origin
]

# Increase limit for forms with many fields (e.g., challenge admin with many records)
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000


# Application definition

INSTALLED_APPS = [
    "clearcache",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.sitemaps",
    "django.contrib.sites",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # 3rd party
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.facebook",
    "allauth.socialaccount.providers.google",
    "corsheaders",
    "crispy_bulma",
    "crispy_forms",
    "django_q",
    "embed_video",
    "markdownx",
    # Local
    "store_project.admin_honeypot",
    "store_project.cardio.apps.CardioConfig",
    "store_project.meso.apps.MesoConfig",
    "store_project.challenges.apps.ChallengesConfig",
    "store_project.exercises.apps.ExercisesConfig",
    "store_project.notifications.apps.NotificationsConfig",
    "store_project.pages.apps.PagesConfig",
    "store_project.payments.apps.PaymentsConfig",
    "store_project.products.apps.ProductsConfig",
    "store_project.users.apps.UsersConfig",
    "store_project.analytics.apps.AnalyticsConfig",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [PROJECT_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "store_project.analytics.context_processors.google_analytics",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database
# https://docs.djangoproject.com/en/3.1/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("SQL_DATABASE", "postgres"),
        "USER": os.environ.get("SQL_USER", "postgres"),
        "PASSWORD": os.environ.get("SQL_PASSWORD", "postgres"),
        "HOST": os.environ.get("SQL_HOST", "localhost"),
        "PORT": os.environ.get("SQL_PORT", "5432"),
    }
}

# dj_database_url
if DATABASE_URL := os.environ.get("DATABASE_URL"):
    DATABASES["default"] = dj_database_url.config(conn_max_age=600, ssl_require=True)  # type: ignore


# Password validation
# https://docs.djangoproject.com/en/3.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",  # noqa: E501
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/3.1/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True


USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.1/howto/static-files/

STATIC_URL = "/staticfiles/"
STATIC_ROOT = APP_DIR / "staticfiles"
MEDIA_URL = "/mediafiles/"
MEDIA_ROOT = APP_DIR / "mediafiles"
STATICFILES_DIRS = [
    PROJECT_DIR / "static",
]
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
AWS_STORAGE_BUCKET_NAME = "masterfit"
AWS_ACCESS_KEY_ID = get_env_var("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = get_env_var("AWS_SECRET_ACCESS_KEY")


# User Management

SITE_ID = 1
AUTH_USER_MODEL = "users.User"
LOGIN_URL = "/accounts/login/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/accounts/login/"
ACCOUNT_LOGOUT_ON_GET = True
LOGIN_REDIRECT_URL = "/users/profile/"

ACCOUNT_USER_DISPLAY = "store_project.users.display.get_email"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*"]
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_EMAIL_CONFIRMATION_EXPIRE_DAYS = 7
ACCOUNT_RATE_LIMITS = {"login_failed": "9/10m/ip,5/5m/key"}
SOCIALACCOUNT_QUERY_EMAIL = True
SOCIALACCOUNT_EMAIL_REQUIRED = True
SOCIALACCOUNT_STORE_TOKENS = False
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
SOCIALACCOUNT_PROVIDERS = {
    "facebook": {
        "APP": {
            "client_id": get_env_var("FB_APP_ID"),
            "secret": get_env_var("FB_SECRET_KEY"),
        },
        "METHOD": "js_sdk",
        "SCOPE": [
            "email",
            "public_profile",
        ],
        "EXCHANGE_TOKEN": False,
        "VERIFIED_EMAIL": False,
    },
    "google": {
        "APP": {
            "client_id": get_env_var("GOOGLE_CLIENT_ID"),
            "secret": get_env_var("GOOGLE_CLIENT_SECRET"),
        },
        "SCOPE": [
            "profile",
            "email",
        ],
        "AUTH_PARAMS": {
            "access_type": "online",
        },
    },
}

# Payments

STRIPE_PUBLISHABLE_KEY = get_env_var("STRIPE_PUBLISHABLE_KEY")
STRIPE_SECRET_KEY = get_env_var("STRIPE_SECRET_KEY")

# Meso agent (Claude proposal engine — B6). Optional: empty key disables the
# agent endpoint (it returns 503) so the app boots and CI runs without creds.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MESO_AGENT_MODEL = os.environ.get("MESO_AGENT_MODEL", "claude-opus-4-8")
# Run the proposal job inline instead of enqueuing it on the django-q cluster
# (Phase 4). Off in real environments (the endpoint returns 202, the cluster runs
# the job, and the frontend polls); tests set it on for deterministic, worker-free
# runs.
MESO_AGENT_RUN_SYNC = os.environ.get("MESO_AGENT_RUN_SYNC", "false").lower() in (
    "1",
    "true",
    "yes",
)

# Meso web push (athlete PWA — decision S3/S7). Optional: with no keys configured,
# push is a silent no-op (subscriptions are still stored, but nothing is sent) so
# the app boots and CI runs without VAPID creds — exactly like the delivery email
# skips an athlete with no address. ``MESO_VAPID_PUBLIC_KEY`` is the base64url
# applicationServerKey the browser subscribes with; ``MESO_VAPID_PRIVATE_KEY`` is
# the base64url (PKCS8 DER) signing key pywebpush uses; ``MESO_VAPID_SUBJECT`` is
# the ``mailto:``/URL contact the push service requires in the VAPID claim.
MESO_VAPID_PUBLIC_KEY = os.environ.get("MESO_VAPID_PUBLIC_KEY", "")
MESO_VAPID_PRIVATE_KEY = os.environ.get("MESO_VAPID_PRIVATE_KEY", "")
MESO_VAPID_SUBJECT = os.environ.get("MESO_VAPID_SUBJECT", "mailto:lance@lancegoyke.com")

# Crispy Forms
CRISPY_ALLOWED_TEMPLATE_PACKS = ("bulma",)
CRISPY_TEMPLATE_PACK = "bulma"

# Cache

DEFAULT_CACHE_TIMEOUT = 604800  # one week

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# TLS Redis (e.g. Heroku Redis, which uses rediss:// with self-signed certs)
# needs relaxed certificate verification. A plain self-hosted redis:// must NOT
# receive this option or the connection errors, so gate it on the URL scheme.
if REDIS_URL.startswith("rediss://"):
    CACHES["default"]["OPTIONS"] = {"ssl_cert_reqs": None}  # type: ignore

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"


# django-q2 — the app-managed scheduler / task queue.
#
# Broker is the Django ORM (the Postgres DB), deliberately NOT Redis: the
# shared box's Redis is small + `noeviction` (cache + sessions), so a task
# backlog there could starve logins. Task volume is tiny (a couple of daily
# invite sweeps today), so a DB-backed broker is more than adequate and keeps
# Redis pressure off. Periodic work lives in `django_q.Schedule` rows
# (registered by a data migration; editable in admin) and is executed by the
# `qcluster` process — one small worker in the production compose stack.
Q_CLUSTER = {
    "name": "fitness-store",
    "orm": "default",  # DB-backed broker (no Redis dependency)
    "workers": 1,  # low task volume — a single worker is plenty
    "timeout": 300,  # kill a task that runs longer than 5 min
    "retry": 600,  # must exceed `timeout` so a live task isn't double-run
    "max_attempts": 1,  # sweeps are idempotent; the next schedule retries
    "catch_up": False,  # after downtime, don't fire every missed run at once
    "poll": 4,  # ORM broker poll interval (s) — easy on the DB
    "save_limit": 250,  # cap retained successful-task rows
    "label": "Scheduled tasks",
}
