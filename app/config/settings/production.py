import os

from .base import *  # noqa

# ALLOWED_HOSTS in .env.prod

INSTALLED_APPS += [  # noqa
    "django_ses",
]

# Email, django-ses

EMAIL_BACKEND = "django_ses.SESBackend"
AWS_SES_ACCESS_KEY_ID = os.environ.get("AWS_SES_ACCESS_KEY_ID")
AWS_SES_SECRET_ACCESS_KEY = os.environ.get("AWS_SES_SECRET_ACCESS_KEY")
AWS_SES_REGION_NAME = os.environ.get("AWS_SES_REGION_NAME")
AWS_SES_REGION_ENDPOINT = os.environ.get("AWS_SES_REGION_ENDPOINT")
AWS_SES_CONFIGURATION_SET = os.environ.get("AWS_SES_CONFIGURATION_SET")

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
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
