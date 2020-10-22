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

# https://docs.djangoproject.com/en/dev/ref/settings/#test-runner
TEST_RUNNER = "django.test.runner.DiscoverRunner"

# Email
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
