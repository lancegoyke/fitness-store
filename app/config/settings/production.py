import os

from .base import *  # noqa

# ALLOWED_HOSTS in .env.prod

INSTALLED_APPS += [  # noqa
    "django_ses",
    "admin_honeypot",
]

# Email, django-ses

EMAIL_BACKEND = "django_ses.SESBackend"
AWS_SES_ACCESS_KEY_ID = os.environ.get("AWS_SES_ACCESS_KEY_ID", "fake598234752934")
AWS_SES_SECRET_ACCESS_KEY = os.environ.get(
    "AWS_SES_SECRET_ACCESS_KEY", "fake423456j234h6k2j5h"
)
AWS_SES_REGION_NAME = os.environ.get("AWS_SES_REGION_NAME", "us-east-2")
AWS_SES_REGION_ENDPOINT = os.environ.get(
    "AWS_SES_REGION_ENDPOINT", "email.us-east-2.amazonaws.com"
)
AWS_SES_CONFIGURATION_SET = os.environ.get("AWS_SES_CONFIGURATION_SET", "Tracking")

# Logging

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {"format": "%(name)-12s %(levelname)-8s %(message)s"},
        "file": {"format": "%(asctime)s %(name)-12s %(levelname)-8s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "console"},
        "file": {
            "level": "DEBUG",
            "class": "logging.FileHandler",
            "formatter": "file",
            "filename": "/tmp/debug.log",
        },
    },
    "loggers": {"django": {"level": "DEBUG", "handlers": ["console", "file"]}},
}

# Security

SECURE_HSTS_SECONDS = 60  # 31536000 is one year, common when things are known to work
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
