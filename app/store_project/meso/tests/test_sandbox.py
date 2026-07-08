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
from store_project.meso import tour
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
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

    def test_starts_with_an_empty_workspace(self):
        """The empty-start flip (guided-tour Phase 2, #430) — no eager load_demo."""
        user = sandbox.create_sandbox()
        assert demo.has_demo(user) is False

    def test_arms_the_guided_tour_at_step_zero(self):
        """The tour populates the workspace instead of an eager load (#430 Phase 2)."""
        user = sandbox.create_sandbox()
        assert CoachProfile.objects.get(user=user).tour_state == {
            "step": 0,
            "status": "active",
        }

    def test_two_sandboxes_are_distinct_and_isolated(self):
        """Loading the same segment for two sandboxes never lets rows collide."""
        a = sandbox.create_sandbox()
        b = sandbox.create_sandbox()
        assert a.pk != b.pk
        demo.load_athletes(a)
        demo.load_athletes(b)
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
    """A sandbox coach for guard/view tests below.

    ``create_sandbox`` itself starts empty (#430 Phase 2 — the guided tour
    populates it step by step); the tests below are about guard/UI behavior
    *on top of* a populated demo workspace (Maya's plan, the group, ...), not
    about the empty-start/tour behavior itself, so this loads the full demo
    explicitly (mirroring the pre-Phase-2 fixture these tests were written
    against) and marks the tour complete — exactly what the real ``tour_skip``
    endpoint does — so these tests' rendered pages don't also carry an active
    tour mount alongside whatever they're actually asserting on.
    """
    coach = sandbox.create_sandbox()
    demo.load_demo(coach)
    tour.complete(CoachProfile.objects.get(user=coach))
    return coach


# ---------------------------------------------------------------------------
# The public entry view — GET /meso/demo/
# ---------------------------------------------------------------------------


class TestSandboxEnterView:
    def test_anonymous_visitor_gets_an_empty_sandbox_with_an_active_tour(self, client):
        resp = client.get(reverse("meso:sandbox_enter"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert "_auth_user_id" in client.session

        from store_project.users.models import User

        user = User.objects.get(pk=client.session["_auth_user_id"])
        assert CoachProfile.objects.filter(user=user).exists()
        assert SandboxSession.objects.filter(user=user).exists()
        # Empty-start flip (#430 Phase 2): no eager demo.load_demo — the
        # guided tour (armed at step 0) populates the workspace instead.
        assert demo.has_demo(user) is False
        assert CoachProfile.objects.get(user=user).tour_state == {
            "step": 0,
            "status": "active",
        }

    def test_follow_redirect_renders_roster_as_coach(self, client):
        resp = client.get(reverse("meso:sandbox_enter"), follow=True)
        assert resp.status_code == 200
        assert b"Roster" in resp.content

    def test_entry_shows_a_single_live_demo_message(self, client):
        """Landing on the roster shows the "live demo" message exactly once.

        The persistent sandbox banner (_meso_base.html) already carries it on
        every screen; a welcome flash on entry duplicated it on the roster
        (issue #425). The banner is the single source of that copy now.

        Carry-over is deferred (S6): signup starts a FRESH workspace, so no
        copy may promise the visitor keeps their sandbox work.
        """
        resp = client.get(reverse("meso:sandbox_enter"), follow=True)
        body = resp.content.decode()
        assert body.lower().count("live demo") == 1  # the banner, not also a flash
        assert "keep your work" not in body

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
        # Loading the same segment for both proves rows never collide even
        # though both sandboxes start empty (#430 Phase 2).
        demo.load_athletes(user_a)
        demo.load_athletes(user_b)
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
        # ``next`` targets the coach-onboarding funnel, NOT the roster: a
        # brand-new signup has no CoachProfile, so RosterView would bounce
        # them to the athlete home — dead-ending the "create an account to
        # run the AI agent" promise on the wrong surface.
        assert "next=%2Fmeso%2Fcoach%2F" in resp.url

    def test_the_funnel_receives_a_fresh_signup(self, client):
        """The become-coach funnel receives a just-signed-up non-coach.

        Renders the start-coaching form — not a bounce — proving the ``next``
        target works for the post-signup authenticated-non-coach shape.
        """
        fresh = UserFactory()  # authenticated, no CoachProfile — post-signup shape
        client.force_login(fresh)

        resp = client.get(reverse("meso:become_coach"))

        assert resp.status_code == 200
        body = resp.content.decode()
        assert reverse("meso:start_coaching") in body  # the CoachProfile-creating POST

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

    def test_real_athlete_cannot_request_a_sandbox_coach(self, client, mailoutbox):
        """The other side of the seam: the requester is real, the *target* isn't.

        A sandbox coach's email is resolvable (it has a ``CoachProfile``) — a
        real athlete submitting it must get exactly the unknown-email response
        (no leak that the address exists), no pending link, and no mail queued
        to the throwaway ``@sandbox.invalid`` address.
        """
        from store_project.meso.models import CoachAthlete

        sandbox_coach = _sandbox_coach()
        athlete = UserFactory()
        client.force_login(athlete)

        resp = client.post(
            reverse("meso:athlete_request_coach"),
            data={"email": sandbox_coach.email},
            follow=True,
        )

        assert not CoachAthlete.objects.filter(
            coach=sandbox_coach, athlete=athlete
        ).exists()
        assert mailoutbox == []
        # Identical to the unknown-coach path: same flash, same landing.
        body = resp.content.decode()
        assert "find a coach with that email" in body

    def test_sandbox_user_cannot_claim_a_real_invite(self, client, mailoutbox):
        """A sandbox user opening a real claim link is logged out, not bound.

        The claim is bearer-token authorized (any authenticated user holding
        the token may accept), so a visitor still logged in as a throwaway
        sandbox account would bind a real coach to a disposable
        ``@sandbox.invalid`` athlete the expiry sweep later deletes — and whose
        deliveries aren't suppressed (the notification guard checks the coach
        side only). Instead the sandbox session is ended and the anonymous
        retry lands on login with ``?next=`` back to the claim, exactly like
        any logged-out invitee.
        """
        from store_project.meso.factories import CoachInviteFactory
        from store_project.meso.models import CoachAthlete
        from store_project.meso.models import CoachInvite

        real_coach = UserFactory()
        CoachProfile.objects.create(user=real_coach)
        invite = CoachInviteFactory(coach=real_coach)
        claim_url = reverse("meso:invite_claim", kwargs={"token": invite.token})

        client.get(reverse("meso:sandbox_enter"))
        sandbox_user_id = client.session["_auth_user_id"]

        resp = client.get(claim_url)

        # Logged out and retried anonymously: back to the same claim URL...
        assert "_auth_user_id" not in client.session
        assert resp.status_code == 302
        assert resp.url == claim_url
        # ...where login_required sends them to login with ?next= back here.
        resp2 = client.get(claim_url)
        assert resp2.status_code == 302
        assert reverse("account_login") in resp2.url
        assert claim_url in resp2.url  # carries ?next=
        # The invite is untouched, no link was bound, nothing was emailed.
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.PENDING
        assert not CoachAthlete.objects.filter(athlete_id=sandbox_user_id).exists()
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
        # Carry-over is deferred (S6): signup starts a FRESH workspace — the
        # banner must not promise the visitor keeps their sandbox work.
        assert "keep your work" not in body

    def test_banner_points_at_the_free_trial_not_the_free_tier(self, client):
        """The banner sells the trial, not the weaker free tier (issue #416).

        Full access lives behind the trial (the free tier caps at 5 agent
        runs/mo); renders from ``CoachSubscription.TRIAL_DAYS`` so a future
        constant change can't leave the banner stale.
        """
        coach = _sandbox_coach()
        client.force_login(coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert "free trial" in body.lower()
        assert f"{CoachSubscription.TRIAL_DAYS}-day free trial" in body

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
    """The composer-vs-signup-gate *render* moved client-side in Phase 2 PR B.

    ``ChatPanel.tsx`` branches on ``meso-designer-flags``'
    ``is_sandbox``/``can_use_agent`` — see ``frontend/designer/CONTRACT.md``.
    Neither the composer nor the signup CTA render server-side any more (both
    are behind the same island mount point), so the server-side seam these
    guard is now the flags payload feeding that client branch, not rendered
    markup — the ``signup_url`` itself is always present in the JSON (it's
    data for a client-side ``if``, not a server-side gate), so the meaningful
    check is ``is_sandbox``/``can_use_agent`` themselves.
    """

    def test_sandbox_coach_gets_is_sandbox_true(self, client):
        coach = _sandbox_coach()
        plan = _sandbox_individual_plan(coach)
        client.force_login(coach)

        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))

        assert resp.context["designer_flags"]["is_sandbox"] is True
        assert b'"is_sandbox": true' in resp.content
        assert reverse("meso:sandbox_signup").encode() in resp.content

    def test_real_coach_with_agent_access_gets_is_sandbox_false(self, client):
        from store_project.meso.factories import CoachAthleteFactory
        from store_project.meso.factories import PlanFactory
        from store_project.meso.models import CoachSubscription

        link = CoachAthleteFactory()
        CoachSubscription.comp(link.coach)
        plan = PlanFactory(relationship=link)
        client.force_login(link.coach)

        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))

        flags = resp.context["designer_flags"]
        assert flags["is_sandbox"] is False
        assert flags["can_use_agent"] is True


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
        # A fresh sandbox starts empty (#430 Phase 2) — load the ``athletes``
        # segment explicitly so this test still covers the leak trap below
        # (the demo athletes are separate User rows the coach delete doesn't
        # cascade to).
        demo.load_athletes(user)
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
        # Empty-start (#430 Phase 2): nothing to preserve here but the row +
        # its (untouched) tour progress.
        assert demo.has_demo(user) is False
        assert CoachProfile.objects.get(user=user).tour_state["status"] == "active"

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
