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

Phase 2 (cleanup + hardening):

- the expiry sweep (``sandbox.expire_sandboxes`` / ``meso_expire_sandboxes`` /
  the ``tasks`` wrapper) — a reaped sandbox must delete the demo-athlete
  ``User`` rows too (they are separate users with no FK cascade from the
  coach), and must never touch a real coach;
- entry hardening — a per-IP rate limit and a global concurrent-sandbox cap
  on ``sandbox_enter``, plus ``X-Robots-Tag: noindex`` (a GET that mints DB
  rows must not be crawled repeatedly).
"""

import json
from unittest import mock

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from store_project.meso import demo
from store_project.meso import sandbox
from store_project.meso import tasks
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachProfile
from store_project.meso.models import MesoGroup
from store_project.meso.models import Plan
from store_project.meso.models import SandboxSession
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Isolate the per-IP sandbox rate counter between tests.

    LocMemCache persists per-process, and every test client shares the default
    ``127.0.0.1`` — without a clear, earlier tests' entries would trip the
    rate limit for later ones.
    """
    cache.clear()
    yield
    cache.clear()


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


# ---------------------------------------------------------------------------
# UI — "Draft with AI" routes to the signup gate instead of a form submit
# ---------------------------------------------------------------------------


class TestDraftWithAiCTAs:
    def test_roster_new_program_offers_the_signup_gate(self, client):
        coach = _sandbox_coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert (
            f'href="{reverse("meso:sandbox_signup")}"' in body
            and "Draft with AI" in body
        )
        assert 'name="draft" value="agent"' not in body

    def test_athlete_profile_offers_the_signup_gate(self, client):
        from store_project.meso.models import CoachAthlete

        coach = _sandbox_coach()
        client.force_login(coach)
        fresh_link = next(
            link
            for link in CoachAthlete.objects.for_coach(coach).filter(is_demo=True)
            if link.working_plan() is None
        )

        body = client.get(
            reverse("meso:athlete", kwargs={"pk": fresh_link.athlete.pk})
        ).content.decode()
        assert (
            f'href="{reverse("meso:sandbox_signup")}"' in body
            and "Draft with AI" in body
        )
        assert 'name="draft" value="agent"' not in body


# ---------------------------------------------------------------------------
# UI — inviting a real athlete and billing are both hidden for sandbox coaches
# ---------------------------------------------------------------------------


class TestRosterInviteAndBillingSurfaces:
    def test_roster_hides_the_real_invite_form_for_a_sandbox_coach(self, client):
        coach = _sandbox_coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert reverse("meso:coach_invite") not in body
        assert "off in the demo" in body.lower()

    def test_roster_shows_the_real_invite_form_for_a_real_coach(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert reverse("meso:coach_invite") in body

    def test_roster_hides_billing_actions_for_a_sandbox_coach(self, client):
        coach = _sandbox_coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert reverse("meso:billing_subscribe") not in body
        assert reverse("meso:billing_start_trial") not in body
        assert "billing is disabled in the demo" in body.lower()

    def test_coach_billing_page_hides_actions_for_a_sandbox_coach(self, client):
        coach = _sandbox_coach()
        client.force_login(coach)

        body = client.get(reverse("meso:billing")).content.decode()
        assert reverse("meso:billing_subscribe") not in body
        assert reverse("meso:billing_start_trial") not in body
        assert "billing is disabled in the demo" in body.lower()


# ---------------------------------------------------------------------------
# UI — "Try the demo" CTAs on the two public marketing pages
# ---------------------------------------------------------------------------


class TestTryTheDemoCTAs:
    def test_landing_offers_the_demo(self, client):
        resp = client.get(reverse("meso:roster"))  # anon → landing.html
        body = resp.content.decode()
        assert f'href="{reverse("meso:sandbox_enter")}"' in body
        assert "try the demo" in body.lower()

    def test_become_coach_offers_the_demo_to_an_anonymous_visitor(self, client):
        body = client.get(reverse("meso:become_coach")).content.decode()
        assert f'href="{reverse("meso:sandbox_enter")}"' in body
        assert "try the demo" in body.lower()


# ---------------------------------------------------------------------------
# Phase 2 — the expiry sweep (sandbox.expire_sandboxes + command + task)
# ---------------------------------------------------------------------------


def _expire(user, hours_ago=1):
    """Backdate a sandbox's expiry so the sweep sees it as overdue."""
    SandboxSession.objects.filter(user=user).update(
        expires_at=timezone.now() - timezone.timedelta(hours=hours_ago)
    )


class TestExpireSandboxes:
    def test_expired_sandbox_is_fully_reaped_including_demo_athletes(self):
        user = sandbox.create_sandbox()
        athlete_ids = [u.pk for u in demo._demo_athletes(user)]
        assert len(athlete_ids) == 5
        _expire(user)

        reaped = sandbox.expire_sandboxes()

        assert reaped == 1
        assert not User.objects.filter(pk=user.pk).exists()
        # The leak trap: the demo athletes are SEPARATE User rows with no FK
        # cascade from the coach — a cascade-only sweep would orphan all five.
        assert not User.objects.filter(pk__in=athlete_ids).exists()
        assert not MesoGroup.objects.filter(coach_id=user.pk).exists()
        assert SandboxSession.objects.count() == 0
        assert CoachProfile.objects.filter(user_id=user.pk).count() == 0

    def test_unexpired_sandbox_is_untouched(self):
        user = sandbox.create_sandbox()  # expires 48h out

        reaped = sandbox.expire_sandboxes()

        assert reaped == 0
        assert User.objects.filter(pk=user.pk).exists()
        assert SandboxSession.objects.filter(user=user).exists()
        assert demo.has_demo(user) is True

    def test_regular_coach_with_demo_data_is_never_touched(self):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        demo.load_demo(coach)
        expired_sandbox = sandbox.create_sandbox()
        _expire(expired_sandbox)

        sandbox.expire_sandboxes()

        # The real coach and their (identical-shaped) demo data survive.
        assert User.objects.filter(pk=coach.pk).exists()
        assert demo.has_demo(coach) is True
        assert list(demo._demo_athletes(coach))  # all five athlete users intact

    def test_now_parameter_moves_the_cutoff(self):
        user = sandbox.create_sandbox()  # expires 48h out

        reaped = sandbox.expire_sandboxes(
            now=timezone.now() + timezone.timedelta(hours=49)
        )

        assert reaped == 1
        assert not User.objects.filter(pk=user.pk).exists()

    def test_idempotent_on_rerun(self):
        user = sandbox.create_sandbox()
        _expire(user)

        assert sandbox.expire_sandboxes() == 1
        assert sandbox.expire_sandboxes() == 0

    def test_one_bad_row_does_not_wedge_the_sweep(self):
        bad = sandbox.create_sandbox()
        good = sandbox.create_sandbox()
        _expire(bad)
        _expire(good)
        real_clear = demo.clear_demo

        def exploding_clear(coach):
            if coach.pk == bad.pk:
                raise RuntimeError("boom")
            return real_clear(coach)

        with mock.patch.object(sandbox.demo, "clear_demo", exploding_clear):
            reaped = sandbox.expire_sandboxes()

        # The good sandbox was reaped despite the bad one blowing up.
        assert reaped == 1
        assert not User.objects.filter(pk=good.pk).exists()
        assert User.objects.filter(pk=bad.pk).exists()  # left for the next run


class TestExpireSandboxesCommand:
    def test_command_reaps_and_reports(self):
        from io import StringIO

        user = sandbox.create_sandbox()
        _expire(user)
        out = StringIO()

        call_command("meso_expire_sandboxes", stdout=out)

        assert not User.objects.filter(pk=user.pk).exists()
        assert "1" in out.getvalue()

    def test_dry_run_reports_without_deleting(self):
        from io import StringIO

        user = sandbox.create_sandbox()
        _expire(user)
        out = StringIO()

        call_command("meso_expire_sandboxes", "--dry-run", stdout=out)

        assert User.objects.filter(pk=user.pk).exists()
        assert SandboxSession.objects.filter(user=user).exists()
        assert "1" in out.getvalue()
        assert "dry run" in out.getvalue().lower()


class TestExpireSandboxesTask:
    def test_task_wrapper_runs_the_sweep(self):
        user = sandbox.create_sandbox()
        _expire(user)

        tasks.expire_sandboxes()

        assert not User.objects.filter(pk=user.pk).exists()


# ---------------------------------------------------------------------------
# Phase 2 — entry hardening: per-IP rate limit + concurrent-sandbox cap
# ---------------------------------------------------------------------------


class TestSandboxEntryRateLimit:
    def _enter(self, ip):
        # A fresh Client per attempt: a successful entry logs the visitor in,
        # and an authenticated revisit resumes rather than creating.
        return Client().get(reverse("meso:sandbox_enter"), REMOTE_ADDR=ip)

    def test_sixth_creation_from_the_same_ip_is_denied(self, settings):
        settings.MESO_SANDBOX_PER_IP_PER_HOUR = 5
        for _ in range(5):
            self._enter("10.0.0.1")
        assert SandboxSession.objects.count() == 5

        resp = self._enter("10.0.0.1")

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert SandboxSession.objects.count() == 5  # nothing new minted

    def test_a_different_ip_is_unaffected(self, settings):
        settings.MESO_SANDBOX_PER_IP_PER_HOUR = 5
        for _ in range(6):
            self._enter("10.0.0.2")
        assert SandboxSession.objects.count() == 5

        self._enter("10.0.0.3")

        assert SandboxSession.objects.count() == 6

    def test_concurrent_cap_denies_creation(self, settings):
        settings.MESO_SANDBOX_MAX_CONCURRENT = 2
        sandbox.create_sandbox()
        sandbox.create_sandbox()

        resp = self._enter("10.0.0.4")

        assert resp.status_code == 302
        assert SandboxSession.objects.count() == 2

    def test_denial_creates_no_rows_and_lands_on_the_landing_page(self, settings):
        settings.MESO_SANDBOX_MAX_CONCURRENT = 0
        users_before = User.objects.count()

        client = Client()
        resp = client.get(
            reverse("meso:sandbox_enter"), REMOTE_ADDR="10.0.0.5", follow=True
        )

        assert User.objects.count() == users_before
        assert SandboxSession.objects.count() == 0
        # Denied anonymous visitor lands back on the public landing page,
        # with the friendly busy message flashed.
        body = resp.content.decode()
        assert "demo is busy" in body.lower()
        assert reverse("meso:become_coach") in body  # landing.html rendered


# ---------------------------------------------------------------------------
# Phase 2 — crawler hardening: noindex the minting GET + robots.txt disallow
# ---------------------------------------------------------------------------


class TestCrawlerHardening:
    def test_successful_entry_is_noindexed(self, client):
        resp = client.get(reverse("meso:sandbox_enter"))
        assert resp["X-Robots-Tag"] == "noindex"

    def test_denied_entry_is_noindexed(self, client, settings):
        settings.MESO_SANDBOX_MAX_CONCURRENT = 0
        resp = client.get(reverse("meso:sandbox_enter"))
        assert resp["X-Robots-Tag"] == "noindex"

    def test_authenticated_resume_is_noindexed(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:sandbox_enter"))
        assert resp["X-Robots-Tag"] == "noindex"

    def test_robots_txt_disallows_the_sandbox_entry(self, client):
        resp = client.get("/robots.txt")
        assert resp.status_code == 200
        assert "Disallow: /meso/demo/" in resp.content.decode()
