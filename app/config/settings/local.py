import socket

from .base import *  # noqa

# ALLOWED_HOSTS in .env.dev

INSTALLED_APPS += [  # noqa F405
    "debug_toolbar",
]

MIDDLEWARE += [  # noqa F405
    "debug_toolbar.middleware.DebugToolbarMiddleware",
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
