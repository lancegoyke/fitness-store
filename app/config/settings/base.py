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
    "embed_video",
    "markdownx",
    # Local
    "store_project.admin_honeypot",
    "store_project.cardio.apps.CardioConfig",
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

# Crispy Forms
CRISPY_ALLOWED_TEMPLATE_PACKS = ("bulma",)
CRISPY_TEMPLATE_PACK = "bulma"

# Cache

DEFAULT_CACHE_TIMEOUT = 604800  # one week

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
    }
}

if ENVIRONMENT == "PRODUCTION":
    # https://devcenter.heroku.com/articles/connecting-heroku-redis#using-the-built-in-redis-backend-support
    CACHES["default"]["OPTIONS"] = {"ssl_cert_reqs": None}  # type: ignore

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"
