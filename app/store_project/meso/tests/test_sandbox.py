"""Public, no-signup ephemeral coach sandbox — Phase 1 (issue #389).

A logged-out visitor to ``/meso/demo/`` lands in a real, populated coach
workspace with no signup: a throwaway ``User`` + ``CoachProfile`` is minted,
seeded via ``demo.load_demo``, and the visitor is logged in as it — every
existing login-gated view, CSRF token, and coach-scoping query just works.
The **one** capability held back is the AI agent, gated behind creating a real
account (that gate is the conversion moment and keeps agent usage attributable
to a real account — the sandbox never calls Anthropic). See
``docs/meso/public-sandbox-demo-plan.md``.

These tests cover:

- the ``SandboxSession`` model (the marker guards/expiry key off);
- ``sandbox.is_sandbox`` / ``sandbox.create_sandbox`` (the module the guards
  and the entry view build on);
- the public entry view (``GET /meso/demo/``) — creates+seeds+logs in an
  anonymous visitor, resumes an authenticated one, isolates concurrent
  visitors, sends no email;
- the hard invariant guards — agent, drafting, delivery notifications,
  invites/requests, and billing are all no-ops for a sandbox coach;
- the UI surfaces — banner, agent gate, hidden billing, hidden real-email
  invite, and the "Try the demo" CTAs;
- the ``sandbox_signup`` conversion hop (logs a sandbox coach out, then hands
  off to allauth signup with ``?next=`` back to the roster).
"""

import pytest
from django.utils import timezone

from store_project.meso import demo
from store_project.meso import sandbox
from store_project.meso.models import CoachProfile
from store_project.meso.models import SandboxSession
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


class TestSandboxSessionModel:
    def test_creates_with_expiry_and_optional_ip(self):
        user = UserFactory()
        expires_at = timezone.now() + timezone.timedelta(hours=48)
        session = SandboxSession.objects.create(
            user=user, expires_at=expires_at, source_ip="203.0.113.4"
        )
        assert session.user == user
        assert session.expires_at == expires_at
        assert session.source_ip == "203.0.113.4"
        assert session.created is not None

    def test_source_ip_is_optional(self):
        user = UserFactory()
        session = SandboxSession.objects.create(
            user=user, expires_at=timezone.now() + timezone.timedelta(hours=48)
        )
        assert session.source_ip is None

    def test_one_session_per_user(self):
        user = UserFactory()
        SandboxSession.objects.create(
            user=user, expires_at=timezone.now() + timezone.timedelta(hours=48)
        )
        with pytest.raises(Exception):  # noqa: B017 — IntegrityError, driver-specific
            SandboxSession.objects.create(
                user=user, expires_at=timezone.now() + timezone.timedelta(hours=48)
            )

    def test_str_mentions_user_and_expiry(self):
        user = UserFactory()
        expires_at = timezone.now() + timezone.timedelta(hours=48)
        session = SandboxSession.objects.create(user=user, expires_at=expires_at)
        assert str(user.pk) in str(session)


# ---------------------------------------------------------------------------
# sandbox.is_sandbox / sandbox.create_sandbox — the module the guards and the
# entry view build on
# ---------------------------------------------------------------------------


class TestIsSandbox:
    def test_false_for_none(self):
        assert sandbox.is_sandbox(None) is False

    def test_false_for_anonymous(self):
        from django.contrib.auth.models import AnonymousUser

        assert sandbox.is_sandbox(AnonymousUser()) is False

    def test_false_for_a_regular_user(self):
        assert sandbox.is_sandbox(UserFactory()) is False

    def test_true_for_a_sandbox_user(self):
        user = UserFactory()
        SandboxSession.objects.create(
            user=user, expires_at=timezone.now() + timezone.timedelta(hours=48)
        )
        assert sandbox.is_sandbox(user) is True


class TestCreateSandbox:
    def test_creates_a_user_with_unusable_password(self):
        user = sandbox.create_sandbox()
        assert user.email.endswith(f"@{sandbox.SANDBOX_EMAIL_DOMAIN}")
        assert user.username == user.email
        assert user.has_usable_password() is False

    def test_creates_a_coach_profile(self):
        user = sandbox.create_sandbox()
        assert CoachProfile.objects.filter(user=user).exists()

    def test_creates_a_sandbox_session_with_ttl_expiry(self, settings):
        settings.MESO_SANDBOX_TTL_HOURS = 48
        before = timezone.now()
        user = sandbox.create_sandbox()
        session = SandboxSession.objects.get(user=user)
        assert session.expires_at - before >= timezone.timedelta(hours=47, minutes=59)
        assert session.expires_at - before <= timezone.timedelta(hours=48, minutes=1)

    def test_records_source_ip(self):
        user = sandbox.create_sandbox(source_ip="203.0.113.9")
        assert SandboxSession.objects.get(user=user).source_ip == "203.0.113.9"

    def test_seeds_demo_data(self):
        user = sandbox.create_sandbox()
        assert demo.has_demo(user) is True

    def test_two_sandboxes_are_distinct_and_isolated(self):
        a = sandbox.create_sandbox()
        b = sandbox.create_sandbox()
        assert a.pk != b.pk
        a_athletes = {u.pk for u in demo._demo_athletes(a)}
        b_athletes = {u.pk for u in demo._demo_athletes(b)}
        assert a_athletes.isdisjoint(b_athletes)

    def test_marks_is_sandbox(self):
        user = sandbox.create_sandbox()
        assert sandbox.is_sandbox(user) is True

    def test_default_ttl_setting_is_48_hours(self):
        from django.conf import settings as django_settings

        assert django_settings.MESO_SANDBOX_TTL_HOURS == 48


def _sandbox_coach():
    """A sandbox coach for guard/view tests below."""
    return sandbox.create_sandbox()
