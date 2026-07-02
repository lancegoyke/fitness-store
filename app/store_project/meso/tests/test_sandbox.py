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

import json
from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from store_project.meso import demo
from store_project.meso import sandbox
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachProfile
from store_project.meso.models import Plan
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


# ---------------------------------------------------------------------------
# The public entry view — GET /meso/demo/
# ---------------------------------------------------------------------------


class TestSandboxEnterView:
    def test_anonymous_visitor_gets_a_seeded_sandbox_and_is_logged_in(self, client):
        resp = client.get(reverse("meso:sandbox_enter"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert "_auth_user_id" in client.session

        from store_project.users.models import User

        user = User.objects.get(pk=client.session["_auth_user_id"])
        assert CoachProfile.objects.filter(user=user).exists()
        assert SandboxSession.objects.filter(user=user).exists()
        assert demo.has_demo(user) is True

    def test_follow_redirect_renders_roster_as_coach(self, client):
        resp = client.get(reverse("meso:sandbox_enter"), follow=True)
        assert resp.status_code == 200
        assert b"Roster" in resp.content

    def test_expiry_is_about_48_hours_out(self, client):
        before = timezone.now()
        client.get(reverse("meso:sandbox_enter"))
        session = SandboxSession.objects.get(user_id=client.session["_auth_user_id"])
        assert session.expires_at - before >= timezone.timedelta(hours=47, minutes=59)
        assert session.expires_at - before <= timezone.timedelta(hours=48, minutes=1)

    def test_two_anonymous_visitors_get_different_isolated_sandboxes(self):
        client_a, client_b = Client(), Client()
        client_a.get(reverse("meso:sandbox_enter"))
        client_b.get(reverse("meso:sandbox_enter"))
        user_a_id = client_a.session["_auth_user_id"]
        user_b_id = client_b.session["_auth_user_id"]
        assert user_a_id != user_b_id

        from store_project.users.models import User

        user_a = User.objects.get(pk=user_a_id)
        user_b = User.objects.get(pk=user_b_id)
        a_athletes = {u.pk for u in demo._demo_athletes(user_a)}
        b_athletes = {u.pk for u in demo._demo_athletes(user_b)}
        assert a_athletes.isdisjoint(b_athletes)

    def test_authenticated_visitor_is_redirected_without_a_new_sandbox(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)

        resp = client.get(reverse("meso:sandbox_enter"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert sandbox.is_sandbox(coach) is False
        assert SandboxSession.objects.count() == 0

    def test_authenticated_sandbox_visitor_resumes_their_own_sandbox(self, client):
        """Revisiting /meso/demo/ mid-session resumes — no second sandbox minted."""
        client.get(reverse("meso:sandbox_enter"))
        first_user_id = client.session["_auth_user_id"]

        resp = client.get(reverse("meso:sandbox_enter"))
        assert resp.status_code == 302
        assert client.session["_auth_user_id"] == first_user_id
        assert SandboxSession.objects.count() == 1

    def test_sends_no_email(self, client, mailoutbox):
        client.get(reverse("meso:sandbox_enter"))
        assert mailoutbox == []

    def test_post_not_allowed(self, client):
        resp = client.post(reverse("meso:sandbox_enter"))
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# sandbox_signup — the conversion hop (allauth bounces authenticated visitors
# off /accounts/signup/, so a sandbox coach must be logged out first)
# ---------------------------------------------------------------------------


class TestSandboxSignupView:
    def test_logs_a_sandbox_coach_out_and_redirects_to_signup(self, client):
        client.get(reverse("meso:sandbox_enter"))
        assert "_auth_user_id" in client.session

        resp = client.get(reverse("meso:sandbox_signup"))
        assert "_auth_user_id" not in client.session
        assert resp.status_code == 302
        assert resp.url.startswith(reverse("account_signup"))
        assert f"next={reverse('meso:roster')}" in resp.url or "next=%2Fmeso%2F" in (
            resp.url
        )

    def test_the_sandbox_user_row_is_not_deleted(self, client):
        client.get(reverse("meso:sandbox_enter"))
        user_id = client.session["_auth_user_id"]

        client.get(reverse("meso:sandbox_signup"))

        from store_project.users.models import User

        assert User.objects.filter(pk=user_id).exists()
        assert SandboxSession.objects.filter(user_id=user_id).exists()

    def test_non_sandbox_authenticated_visitor_is_also_sent_to_signup(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)

        resp = client.get(reverse("meso:sandbox_signup"))
        assert resp.status_code == 302
        assert resp.url.startswith(reverse("account_signup"))
        # Harmless — not logged out, since they aren't a sandbox user.
        assert "_auth_user_id" in client.session

    def test_anonymous_visitor_is_sent_to_signup(self, client):
        resp = client.get(reverse("meso:sandbox_signup"))
        assert resp.status_code == 302
        assert resp.url.startswith(reverse("account_signup"))

    def test_post_not_allowed(self, client):
        resp = client.post(reverse("meso:sandbox_signup"))
        assert resp.status_code == 405


def _sandbox_individual_plan(coach):
    """The seeded individual plan (Maya's) on the sandbox coach's own demo data."""
    return (
        Plan.objects.filter(relationship__coach=coach, relationship__is_demo=True)
        .exclude(source_group__isnull=False)
        .first()
    )


# ---------------------------------------------------------------------------
# Guards — the hard invariants: no agent calls, no email/push, no billing
# ---------------------------------------------------------------------------


class TestAgentGuard:
    def test_sandbox_coach_gets_a_signup_gate_not_the_agent(self, client):
        coach = _sandbox_coach()
        plan = _sandbox_individual_plan(coach)
        client.force_login(coach)

        resp = client.post(
            reverse("meso:api_plan_agent", kwargs={"plan_id": plan.pk}),
            data=json.dumps({"instruction": "Add more volume."}),
            content_type="application/json",
        )

        assert resp.status_code == 403
        body = resp.json()
        assert body["signup_required"] is True
        assert body["signup_url"] == reverse("meso:sandbox_signup")
        assert AgentProposalBatch.objects.count() == 0

    def test_the_gate_fires_before_the_no_api_key_check(self, client, settings):
        """The guard beats the 503 'not configured' shape (proves fire order)."""
        settings.ANTHROPIC_API_KEY = ""
        coach = _sandbox_coach()
        plan = _sandbox_individual_plan(coach)
        client.force_login(coach)

        resp = client.post(
            reverse("meso:api_plan_agent", kwargs={"plan_id": plan.pk}),
            data=json.dumps({"instruction": "Add more volume."}),
            content_type="application/json",
        )

        assert resp.status_code == 403
        assert resp.status_code != 503


class _AlwaysDraftsClient:
    """A working fake agent client.

    Proves the guard blocks it, not just a missing API key (mirrors
    ``test_plan_draft.DraftingClient``).
    """

    model = "claude-opus-4-8-test"

    def propose(self, *, context, instruction):
        changes = [
            {
                "kind": "add",
                "session_id": session["id"],
                "title": f"Add accessory to {session['name']}",
                "rationale": "Drafted accessory work.",
                "new_name": "Romanian Deadlift",
                "new_sets": "3",
                "new_reps": "8-10",
                "new_rpe": "7",
            }
            for session in context["plan"]["program"]
        ]
        return {"summary": "Drafted an initial training week.", "changes": changes}


class TestDraftGuard:
    def test_plan_create_draft_makes_no_batch_for_a_sandbox_coach(
        self, client, monkeypatch
    ):
        from store_project.meso.agent import client as agent_client_module
        from store_project.meso.models import CoachAthlete

        monkeypatch.setattr(
            agent_client_module, "get_default_client", lambda: _AlwaysDraftsClient()
        )
        coach = _sandbox_coach()
        client.force_login(coach)

        # Lena is seeded with no plan (only Maya gets one; the other three are
        # group members with a *shared* plan) — a fresh target for plan_create.
        fresh_link = next(
            link
            for link in CoachAthlete.objects.for_coach(coach).filter(is_demo=True)
            if link.working_plan() is None
        )

        resp = client.post(
            reverse("meso:plan_create", kwargs={"pk": fresh_link.athlete.pk}),
            data={"draft": "agent"},
        )

        assert resp.status_code == 302
        assert fresh_link.working_plan() is not None  # the plan still gets built
        assert AgentProposalBatch.objects.count() == 0


class TestNotifyGuard:
    def test_sandbox_delivery_sends_no_email_or_push(
        self, client, mailoutbox, django_capture_on_commit_callbacks
    ):
        coach = _sandbox_coach()
        plan = _sandbox_individual_plan(coach)
        client.force_login(coach)

        with (
            mock.patch(
                "store_project.meso.views.meso_push.notify_week_delivered"
            ) as push_mock,
            django_capture_on_commit_callbacks(execute=True),
        ):
            resp = client.post(
                reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})
            )

        assert resp.status_code == 201
        assert mailoutbox == []
        push_mock.assert_not_called()


class TestInviteAndRequestGuards:
    def test_coach_invite_is_disabled_for_a_sandbox_coach(self, client, mailoutbox):
        from store_project.meso.models import CoachInvite

        coach = _sandbox_coach()
        client.force_login(coach)

        resp = client.post(
            reverse("meso:coach_invite"), data={"email": "real.person@example.com"}
        )

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert CoachInvite.objects.count() == 0
        assert mailoutbox == []

    def test_coach_invite_resend_is_disabled_for_a_sandbox_coach(
        self, client, mailoutbox
    ):
        from store_project.meso.factories import CoachInviteFactory

        coach = _sandbox_coach()
        # A real invite from before the sandbox guard existed (or one somehow
        # created) must not be resent either.
        invite = CoachInviteFactory(coach=coach)
        client.force_login(coach)

        resp = client.post(
            reverse("meso:coach_invite_resend", kwargs={"token": invite.token})
        )

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert mailoutbox == []
        invite.refresh_from_db()
        assert invite.status == invite.Status.PENDING  # untouched, not re-armed

    def test_athlete_request_coach_is_disabled_for_a_sandbox_coach(
        self, client, mailoutbox
    ):
        from store_project.meso.models import CoachAthlete

        coach = _sandbox_coach()
        other_coach = UserFactory()
        CoachProfile.objects.create(user=other_coach)
        client.force_login(coach)

        resp = client.post(
            reverse("meso:athlete_request_coach"), data={"email": other_coach.email}
        )

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert not CoachAthlete.objects.filter(coach=other_coach).exists()
        assert mailoutbox == []


class TestBillingGuards:
    def test_subscribe_is_disabled_for_a_sandbox_coach(self, client, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach = _sandbox_coach()
        client.force_login(coach)

        with mock.patch(
            "store_project.meso.views.billing_gateway.create_subscription_checkout_session"
        ) as create:
            resp = client.post(reverse("meso:billing_subscribe"))

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        create.assert_not_called()

    def test_portal_is_disabled_for_a_sandbox_coach(self, client):
        coach = _sandbox_coach()
        coach.stripe_customer_id = "cus_fake"
        coach.save(update_fields=["stripe_customer_id"])
        client.force_login(coach)

        with mock.patch(
            "store_project.meso.views.billing_gateway.create_billing_portal_session"
        ) as create:
            resp = client.post(reverse("meso:billing_portal"))

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        create.assert_not_called()

    def test_start_trial_is_disabled_for_a_sandbox_coach(self, client):
        from store_project.meso.models import CoachSubscription

        coach = _sandbox_coach()
        client.force_login(coach)

        resp = client.post(reverse("meso:billing_start_trial"))

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert not CoachSubscription.objects.filter(
            coach=coach, status=CoachSubscription.Status.TRIALING
        ).exists()


# ---------------------------------------------------------------------------
# UI — the persistent banner, hidden billing nav link
# ---------------------------------------------------------------------------


class TestSandboxBanner:
    def test_roster_shows_the_banner_for_a_sandbox_coach(self, client):
        coach = _sandbox_coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert "live demo" in body.lower()
        assert reverse("meso:sandbox_signup") in body

    def test_roster_shows_no_banner_for_a_real_coach(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert "live demo" not in body.lower()

    def test_navlinks_hide_billing_for_a_sandbox_coach(self, client):
        coach = _sandbox_coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert ">Billing</a>" not in body

    def test_navlinks_show_billing_for_a_real_coach(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert ">Billing</a>" in body


# ---------------------------------------------------------------------------
# UI — the designer's agent composer becomes a signup gate
# ---------------------------------------------------------------------------


class TestDesignerSignupGate:
    def test_sandbox_coach_sees_the_signup_gate_not_the_composer(self, client):
        coach = _sandbox_coach()
        plan = _sandbox_individual_plan(coach)
        client.force_login(coach)

        body = client.get(
            reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})
        ).content.decode()

        assert 'data-testid="agent-composer-input"' not in body
        assert reverse("meso:sandbox_signup") in body

    def test_real_coach_with_agent_access_sees_the_composer(self, client):
        from store_project.meso.factories import CoachAthleteFactory
        from store_project.meso.factories import PlanFactory
        from store_project.meso.models import CoachSubscription

        link = CoachAthleteFactory()
        CoachSubscription.comp(link.coach)
        plan = PlanFactory(relationship=link)
        client.force_login(link.coach)

        body = client.get(
            reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})
        ).content.decode()

        assert 'data-testid="agent-composer-input"' in body
