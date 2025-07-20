import os
import socket

# Set default AWS credentials for local development
os.environ.setdefault("AWS_ACCESS_KEY_ID", "local-access-key")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local-secret-key")

from .base import *  # noqa

# Use the local filesystem instead of S3 for storage
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# ALLOWED_HOSTS in .env.dev

INSTALLED_APPS += [  # noqa F405
    "debug_toolbar",
    "django_browser_reload",
]

MIDDLEWARE += [  # noqa F405
    "debug_toolbar.middleware.DebugToolbarMiddleware",
    "django_browser_reload.middleware.BrowserReloadMiddleware",
]

INTERNAL_IPS = [
    "127.0.0.1",
]


hostname, _, ips = socket.gethostbyname_ex(socket.gethostname())
INTERNAL_IPS += [".".join(ip.split(".")[:-1] + ["1"]) for ip in ips]

# Email
# EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
EMAIL_FILE_PATH = APP_DIR / "tmp" / "dev-emails"  # noqa F405
