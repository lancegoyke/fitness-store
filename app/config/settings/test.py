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
os.environ.setdefault("TURNSTILE_SITE_KEY", "test-turnstile-site-key")
os.environ.setdefault("TURNSTILE_SECRET_KEY", "test-turnstile-secret-key")
os.environ.setdefault("TURNSTILE_ENDPOINT", "https://test-turnstile-endpoint.com")

from .base import *  # noqa

# Import these after base settings to prevent import order issues
import unittest.mock  # noqa
import requests  # noqa
import dj_database_url  # noqa
import stripe  # noqa

# TESTING
# ------------------------------------------------------------------------------
# Run the Meso agent proposal job inline (no background thread) so tests are
# deterministic — the batch is resolved by the time dispatch returns.
MESO_AGENT_RUN_SYNC = True

# pytest-django forces DEBUG off, so the strict default in base.py would be
# off here too. Tests are where an unknown event name has to fail (#509).
ANALYTICS_STRICT_EVENT_NAMES = True

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


def _autospec_stripe_mock(real_method, **return_attrs):
    """Autospec the real stripe method so tests can't hide a signature bug.

    A wrong-keyword call (e.g. the `sid=`/`id=` mismatches from #548) raises
    TypeError here instead of silently passing, the way a bare Mock() would.
    """
    return unittest.mock.create_autospec(
        real_method, return_value=unittest.mock.Mock(**return_attrs)
    )


# Mock all Stripe API calls
stripe.Product.create = _autospec_stripe_mock(stripe.Product.create, id="prod_test")
stripe.Product.modify = _autospec_stripe_mock(stripe.Product.modify, id="prod_test")
stripe.Product.retrieve = _autospec_stripe_mock(stripe.Product.retrieve, id="prod_test")
stripe.Price.create = _autospec_stripe_mock(stripe.Price.create, id="price_test")
stripe.Price.modify = _autospec_stripe_mock(stripe.Price.modify, id="price_test")
stripe.Price.retrieve = _autospec_stripe_mock(stripe.Price.retrieve, id="price_test")
stripe.Customer.create = _autospec_stripe_mock(stripe.Customer.create, id="cus_test")
stripe.Customer.retrieve = _autospec_stripe_mock(
    stripe.Customer.retrieve, id="cus_test"
)
stripe.checkout.Session.create = unittest.mock.Mock(return_value={"id": "cs_test"})
stripe.checkout.Session.list_line_items = unittest.mock.Mock(
    return_value=unittest.mock.Mock(
        data=[unittest.mock.Mock(description="Test Product", amount_total=1000)]
    )
)
# Safe defaults for the #556 "never open a second subscription" checks: an
# empty result (no open Stripe subscriptions / Checkout Sessions) unless a
# test patches these to something else. ``auto_paging_iter`` must work on
# EVERY call, not just once — a ``Mock(side_effect=lambda: iter([]))`` builds
# a fresh empty iterator each time, unlike a bare exhausted iterator.
stripe.Subscription.list = _autospec_stripe_mock(
    stripe.Subscription.list,
    auto_paging_iter=unittest.mock.Mock(side_effect=lambda: iter([])),
)
stripe.checkout.Session.list = _autospec_stripe_mock(
    stripe.checkout.Session.list,
    auto_paging_iter=unittest.mock.Mock(side_effect=lambda: iter([])),
)
stripe.checkout.Session.expire = _autospec_stripe_mock(stripe.checkout.Session.expire)
# Default: the ordinary post-Checkout path (a real completed session) works
# even in a test that never mocks this itself — the ``?billing=success``
# pending-state check (#556 round 2) retrieves the Checkout Session it
# started to confirm it actually completed. A test asserting the abandoned/
# failure paths patches this locally to something else.
stripe.checkout.Session.retrieve = _autospec_stripe_mock(
    stripe.checkout.Session.retrieve, status="complete"
)
stripe.Webhook.construct_event = unittest.mock.Mock(
    return_value={
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_test", "metadata": {}}},
    }
)

# CLOUDFLARE TURNSTILE
# ------------------------------------------------------------------------------
# The Django test client serves requests as "testserver", so that is the
# hostname a token would legitimately carry here.
TURNSTILE_ALLOWED_HOSTNAMES = ["testserver"]

# Mock requests.post so no test reaches Cloudflare's siteverify. The default is a
# well-formed pass; tests that need a rejection patch
# ``store_project.pages.turnstile.requests.post`` themselves.
requests.post = unittest.mock.Mock(
    return_value=unittest.mock.Mock(
        json=lambda: {
            "success": True,
            "hostname": "testserver",
            "action": "contact",
            "error-codes": [],
        }
    )
)

# DATABASE
# ------------------------------------------------------------------------------
# Use fast in-memory SQLite database for testing by default.
# Benefits:
# - Much faster than file-based databases (no disk I/O)
# - Isolated - each test run gets a fresh database
# - No cleanup required - database disappears when process ends
# https://docs.djangoproject.com/en/dev/ref/settings/#databases
#
# The production/dev DB is PostgreSQL, and a small number of behaviors
# (deferred-constraint enforcement, in particular — see
# meso/tests/test_migration_0040_pending_triggers.py) only exist on Postgres
# and cannot be exercised on SQLite at all. ``TEST_DATABASE_URL`` is an
# optional escape hatch: set it (e.g. in a dedicated CI job) to point the
# suite at a real Postgres instead. Left unset, behavior is byte-for-byte
# identical to before — this is the default local (`uv run pytest`) and main
# CI path, and it must stay fast and hermetic.
if TEST_DATABASE_URL := os.environ.get("TEST_DATABASE_URL"):
    DATABASES = {"default": dj_database_url.parse(TEST_DATABASE_URL, conn_max_age=600)}
else:
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

# SES → SNS event webhook (#507)
# ------------------------------------------------------------------------------
# The topic guard (`ScopedSESEventWebhookView.verify_event_message`) rejects
# any notification whose `TopicArn` isn't allow-listed here. This must match
# the constant `test_ses_webhook.SNS_TOPIC_ARN` the webhook tests' SNS
# envelope helper uses, or every "allow-listed" test starts failing 400.
AWS_SES_EVENT_TOPIC_ARNS = ["arn:aws:sns:us-east-2:497780720908:EmailOpens"]

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
