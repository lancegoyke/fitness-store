"""With these settings, tests run faster."""

import os

# Set environment variables for testing BEFORE importing base settings
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test-access-key")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test-secret-key")
os.environ.setdefault("FB_APP_ID", "test-fb-app-id")
os.environ.setdefault("FB_SECRET_KEY", "test-fb-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-secret")
os.environ.setdefault("STRIPE_ENDPOINT_SECRET", "test-stripe-endpoint-secret")
os.environ.setdefault("STRIPE_PUBLISHABLE_KEY", "test-stripe-publishable-key")
os.environ.setdefault("STRIPE_SECRET_KEY", "test-stripe-secret-key")
os.environ.setdefault("MESO_STRIPE_WEBHOOK_SECRET", "test-meso-webhook-secret")
os.environ.setdefault("G_RECAPTCHA_SITE_KEY", "test-recaptcha-site-key")
os.environ.setdefault("G_RECAPTCHA_SECRET_KEY", "test-recaptcha-secret-key")
os.environ.setdefault("G_RECAPTCHA_ENDPOINT", "https://test-recaptcha-endpoint.com")

from .base import *  # noqa

# Import these after base settings to prevent import order issues
import unittest.mock  # noqa
import requests  # noqa
import stripe  # noqa

# TESTING
# ------------------------------------------------------------------------------
# Run the Meso agent proposal job inline (no background thread) so tests are
# deterministic — the batch is resolved by the time dispatch returns.
MESO_AGENT_RUN_SYNC = True

# Meso web push: a real (ephemeral, test-only) VAPID keypair so the signing path
# in ``meso.push`` actually runs under test. The network send itself is mocked —
# these keys never reach a real push service. The public value is a base64url
# applicationServerKey; the private value is its base64url PKCS8 DER.
MESO_VAPID_PUBLIC_KEY = (
    "BGHM4CGuxntiwQWPBTFdfjMsWpqiIjDLriWlfxSCk-_D"
    "iAcJ0ttNeSR3CJNr0GcktI3le-JgEb7ydvDoQEpUmd0"
)
MESO_VAPID_PRIVATE_KEY = (
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgIKQKjrxm3qC3Ja7C2XVf"
    "vzGySCvOe4gwCL9bJhcKlZmhRANCAARhzOAhrsZ7YsEFjwUxXX4zLFqaoiIwy64lpX8U"
    "gpPvw4gHCdLbTXkkdwiTa9BnJLSN5XviYBG-8nbw6EBKVJnd"
)
MESO_VAPID_SUBJECT = "mailto:test@example.com"

# Mock Stripe API calls for testing
# Using unittest.mock to prevent real API calls during testing

# Mock all Stripe API calls
stripe.Product.create = unittest.mock.Mock(
    return_value=unittest.mock.Mock(id="prod_test")
)
stripe.Product.modify = unittest.mock.Mock(
    return_value=unittest.mock.Mock(id="prod_test")
)
stripe.Product.retrieve = unittest.mock.Mock(
    return_value=unittest.mock.Mock(id="prod_test")
)
stripe.Price.create = unittest.mock.Mock(
    return_value=unittest.mock.Mock(id="price_test")
)
stripe.Price.modify = unittest.mock.Mock(
    return_value=unittest.mock.Mock(id="price_test")
)
stripe.Price.retrieve = unittest.mock.Mock(
    return_value=unittest.mock.Mock(id="price_test")
)
stripe.Customer.create = unittest.mock.Mock(
    return_value=unittest.mock.Mock(id="cus_test")
)
stripe.Customer.retrieve = unittest.mock.Mock(
    return_value=unittest.mock.Mock(id="cus_test")
)
stripe.checkout.Session.create = unittest.mock.Mock(return_value={"id": "cs_test"})
stripe.checkout.Session.list_line_items = unittest.mock.Mock(
    return_value=unittest.mock.Mock(
        data=[unittest.mock.Mock(description="Test Product", amount_total=1000)]
    )
)
stripe.Webhook.construct_event = unittest.mock.Mock(
    return_value={
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_test", "metadata": {}}},
    }
)

# Mock requests.post for reCAPTCHA testing
requests.post = unittest.mock.Mock(
    return_value=unittest.mock.Mock(json=lambda: {"success": True})
)

# DATABASE
# ------------------------------------------------------------------------------
# Use fast in-memory SQLite database for testing
# Benefits:
# - Much faster than file-based databases (no disk I/O)
# - Isolated - each test run gets a fresh database
# - No cleanup required - database disappears when process ends
# https://docs.djangoproject.com/en/dev/ref/settings/#databases
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# CACHES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#caches
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "",
    }
}

# PASSWORDS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#password-hashers
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# EMAIL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#email-backend
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# DJANGO-Q
# ------------------------------------------------------------------------------
# Run any enqueued task inline and never spin up a cluster during tests.
# (timeout/retry kept consistent — retry > timeout — to avoid django-q's
# misconfiguration warning leaking into test output.)
Q_CLUSTER = {
    "name": "fitness-store-test",
    "orm": "default",
    "sync": True,
    "timeout": 30,
    "retry": 60,
}

# Your stuff...
# ------------------------------------------------------------------------------

WHITENOISE_AUTOREFRESH = True

# STATIC FILES
# ------------------------------------------------------------------------------
# Disable WhiteNoise for testing and use Django's default static files handling
STATIC_URL = "/static/"

# Override STORAGES to use Django's default static files handling for tests
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Remove WhiteNoise from middleware for tests
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # "whitenoise.middleware.WhiteNoiseMiddleware",  # Disabled for tests
    "allauth.account.middleware.AccountMiddleware",
    "django_browser_reload.middleware.BrowserReloadMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
