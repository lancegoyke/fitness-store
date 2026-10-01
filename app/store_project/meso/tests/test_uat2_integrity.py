"""UAT round 2 integrity follow-ups: #658, #659, #668, #670, #671 (meso half).

Each class names its issue. The notifications half of #671 (the From display
name) lives in ``notifications/tests/test_client_email_identity.py``.
"""

import re
from unittest import mock

import pytest
from django.core import mail
from django.db import transaction
from django.urls import reverse

from store_project.meso.agent import service
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.meso.models import ProposedChange
from store_project.meso.tests.test_agent_validation import make_plan
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db

ACTIVE = CoachAthlete.Status.ACTIVE
WAITING = CoachAthlete.Status.ACCEPTED_WAITING
ENDED = CoachAthlete.Status.ENDED
PASSWORD = "S3cure-pass-phrase-9"


class CountingClient:
    model = "test-model"

    def __init__(self, result=None, on_propose=None):
        self.calls = 0
        self._result = result or {"summary": "ok", "changes": []}
        self._on_propose = on_propose

    def propose(self, *, context, instruction):
        self.calls += 1
        if self._on_propose:
            self._on_propose()
        return self._result


# ---------------------------------------------------------------------------
# #658 — the queued agent run is re-qualified against the relationship
# ---------------------------------------------------------------------------


class TestAgentJobRequalifiesRelationship:
    def _batch(self, plan):
        return service.create_drafting_batch(
            plan,
            "tweak it",
            coach=plan.relationship.coach,
            mesocycle=plan.mesocycles.first(),
        )

    def test_ended_link_aborts_before_the_provider_call(self):
        plan, _, _ = make_plan()
        batch = self._batch(plan)
        plan.relationship.end(by="coach")
        client = CountingClient()

        service.run_proposal_job(batch.pk, client=client)

        batch.refresh_from_db()
        assert client.calls == 0
        assert batch.status == AgentProposalBatch.Status.FAILED
        assert "no longer active" in batch.error
        assert not ProposedChange.objects.filter(batch=batch).exists()

    def test_waiting_link_aborts_too(self):
        plan, _, _ = make_plan()
        batch = self._batch(plan)
        CoachAthlete.objects.filter(pk=plan.relationship_id).update(status=WAITING)
        client = CountingClient()

        service.run_proposal_job(batch.pk, client=client)

        batch.refresh_from_db()
        assert client.calls == 0
        assert batch.status == AgentProposalBatch.Status.FAILED

    def test_link_ended_during_the_provider_call_persists_nothing(self):
        plan, _, presc = make_plan()
        batch = self._batch(plan)
        link = plan.relationship
        result = {
            "summary": "must never be seen",
            "changes": [
                {
                    "kind": "swap",
                    "prescription_id": presc.pk,
                    "title": "Back Squat → Box Squat",
                    "before": "Back Squat",
                    "after": "Box Squat",
                    "rationale": "x",
                    "introduces_exercise": "Box Squat",
                }
            ],
        }
        client = CountingClient(result, on_propose=lambda: link.end(by="coach"))

        service.run_proposal_job(batch.pk, client=client)

        batch.refresh_from_db()
        assert client.calls == 1
        assert batch.status == AgentProposalBatch.Status.FAILED
        assert batch.summary != "must never be seen"
        assert not ProposedChange.objects.filter(batch=batch).exists()

    def test_self_coaching_still_runs(self):
        coach = UserFactory()
        CoachSubscription.comp(coach)
        link = CoachAthlete.add_self(coach)
        plan, _, _ = make_plan()
        # Re-home the fixture plan onto the self link.
        Plan.objects.filter(pk=plan.pk).update(relationship=link, owner=coach)
        plan.refresh_from_db()
        batch = self._batch(plan)
        client = CountingClient()

        service.run_proposal_job(batch.pk, client=client)

        batch.refresh_from_db()
        assert client.calls == 1
        assert batch.status == AgentProposalBatch.Status.PENDING


# ---------------------------------------------------------------------------
# #659 — a seat opening activates a waiting athlete; the athlete can leave
# ---------------------------------------------------------------------------


def _coach_with_full_seat_and_waiter():
    coach = UserFactory(name="Alex Kim", email="alex@example.com")
    CoachProfileFactory(user=coach, display_name="Alex Kim")
    active = CoachAthleteFactory(
        coach=coach, athlete=UserFactory(name="Riley Roe"), status=ACTIVE
    )
    waiter = CoachAthleteFactory(
        coach=coach, athlete=UserFactory(name="Casey Lee"), status=WAITING
    )
    return coach, active, waiter


class TestSeatOpensActivatesWaiting:
    def test_coach_ending_an_active_athlete_activates_the_waiter(
        self, client, django_capture_on_commit_callbacks
    ):
        coach, active, waiter = _coach_with_full_seat_and_waiter()
        client.force_login(coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(
                reverse("meso:relationship_end", args=[active.token]), {"confirm": "1"}
            )
        waiter.refresh_from_db()
        assert waiter.status == ACTIVE

    def test_athlete_ending_their_own_link_activates_the_waiter(
        self, client, django_capture_on_commit_callbacks
    ):
        coach, active, waiter = _coach_with_full_seat_and_waiter()
        client.force_login(active.athlete)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(
                reverse("meso:relationship_end", args=[active.token]), {"confirm": "1"}
            )
        waiter.refresh_from_db()
        assert waiter.status == ACTIVE

    def test_coach_removing_a_waiting_acceptance_runs_the_hook(
        self, client, django_capture_on_commit_callbacks
    ):
        # A stuck state (no active seats, two waiting): removing the first must
        # let the hook seat the next. Proves decline calls activate_waiting.
        coach = UserFactory()
        first = CoachAthleteFactory(coach=coach, status=WAITING)
        second = CoachAthleteFactory(coach=coach, status=WAITING)
        client.force_login(coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("meso:invite_decline", args=[first.token]))
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.status == CoachAthlete.Status.DECLINED
        assert second.status == ACTIVE

    def test_hook_failure_never_fails_the_request(
        self, client, django_capture_on_commit_callbacks
    ):
        coach, active, waiter = _coach_with_full_seat_and_waiter()
        client.force_login(coach)
        with mock.patch(
            "store_project.meso.billing.activation.activate_waiting",
            side_effect=RuntimeError("boom"),
        ):
            with django_capture_on_commit_callbacks(execute=True):
                resp = client.post(
                    reverse("meso:relationship_end", args=[active.token]),
                    {"confirm": "1"},
                )
        assert resp.status_code == 302
        active.refresh_from_db()
        assert active.status == ENDED


class TestAthleteLeavesWaiting:
    def _url(self, link):
        return reverse("meso:relationship_leave", args=[link.token])

    def test_athlete_home_offers_a_leave_form(self, client):
        coach, _, waiter = _coach_with_full_seat_and_waiter()
        client.force_login(waiter.athlete)
        html = client.get(reverse("meso:athlete_home")).content.decode()
        assert re.search(r'<form[^>]*action="%s"' % re.escape(self._url(waiter)), html)

    def test_athlete_leaves(self, client, django_capture_on_commit_callbacks):
        coach, _, waiter = _coach_with_full_seat_and_waiter()
        client.force_login(waiter.athlete)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(self._url(waiter), {"confirm": "1"})
        assert resp.status_code == 302
        waiter.refresh_from_db()
        assert waiter.status == ENDED
        assert waiter.ended_by == "athlete"
        assert mail.outbox == []

    def test_requires_the_confirm_marker(self, client):
        coach, _, waiter = _coach_with_full_seat_and_waiter()
        client.force_login(waiter.athlete)
        client.post(self._url(waiter))
        waiter.refresh_from_db()
        assert waiter.status == WAITING

    def test_foreign_user_and_coach_cannot(self, client):
        coach, _, waiter = _coach_with_full_seat_and_waiter()
        for who in (UserFactory(), coach):
            client.force_login(who)
            resp = client.post(self._url(waiter), {"confirm": "1"})
            assert resp.status_code in (403, 404)
            waiter.refresh_from_db()
            assert waiter.status == WAITING

    def test_not_available_on_an_active_link(self, client):
        coach, active, _ = _coach_with_full_seat_and_waiter()
        client.force_login(active.athlete)
        resp = client.post(self._url(active), {"confirm": "1"})
        assert resp.status_code in (403, 404)
        active.refresh_from_db()
        assert active.status == ACTIVE


# ---------------------------------------------------------------------------
# #668 — template_create re-checks the invite under lock
# ---------------------------------------------------------------------------


class TestTemplateCreateRevokedInvite:
    def test_invite_revoked_between_lookup_and_transaction_is_unlinked(self, client):
        coach = UserFactory(email="sam@example.com")
        CoachProfileFactory(user=coach)
        invite, _ = CoachInvite.open_for(
            coach=coach, email="jordan@example.com", label="Jordan"
        )
        client.force_login(coach)

        real_atomic = transaction.atomic
        state = {"done": False}

        def revoking_atomic(*args, **kwargs):
            if not state["done"]:
                state["done"] = True
                CoachInvite.objects.filter(pk=invite.pk).update(
                    status=CoachInvite.Status.REVOKED
                )
            return real_atomic(*args, **kwargs)

        with mock.patch.object(transaction, "atomic", revoking_atomic):
            resp = client.post(
                reverse("meso:template_create"), {"invite": str(invite.token)}
            )

        assert resp.status_code == 302
        tpl = Plan.objects.get(is_template=True, owner=coach)
        assert tpl.for_invite is None

    def test_still_pending_invite_is_linked(self, client):
        coach = UserFactory(email="sam@example.com")
        CoachProfileFactory(user=coach)
        invite, _ = CoachInvite.open_for(
            coach=coach, email="jordan@example.com", label="Jordan"
        )
        client.force_login(coach)
        client.post(reverse("meso:template_create"), {"invite": str(invite.token)})
        assert Plan.objects.get(is_template=True, owner=coach).for_invite == invite


# ---------------------------------------------------------------------------
# #670 — the claim flag expires and dies with an unrelated login
# ---------------------------------------------------------------------------


def _invite(coach=None):
    coach = coach or UserFactory(name="Sam Rivera")
    invite, _ = CoachInvite.open_for(
        coach=coach, email="jordan.ellis@example.com", label="Jordan Ellis"
    )
    return invite


def _claim_url(invite):
    return reverse("meso:invite_claim", kwargs={"token": invite.token})


def _existing_user(email="existing@example.com"):
    user = UserFactory(email=email)
    user.set_password(PASSWORD)
    user.save()
    return user


class TestClaimFlagLifetime:
    NOW = 1_800_000_000.0

    def test_flag_expires_after_two_hours(self, client):
        invite = _invite()
        athlete = _existing_user()
        with mock.patch("store_project.meso.claim_session.time.time") as now:
            now.return_value = self.NOW
            client.get(_claim_url(invite))
            client.force_login(athlete)
            now.return_value = self.NOW + 2 * 3600 + 5
            resp = client.get(_claim_url(invite))
        assert resp.status_code == 200
        assert b"Accept invite" in resp.content
        assert not CoachAthlete.objects.filter(athlete=athlete).exists()

    def test_flag_valid_inside_the_window(self, client):
        invite = _invite()
        athlete = _existing_user()
        with mock.patch("store_project.meso.claim_session.time.time") as now:
            now.return_value = self.NOW
            client.get(_claim_url(invite))
            # A real allauth login heading back to the claim page: a bare
            # ``force_login`` is a non-claim-bound login and clears the flag (#677).
            client.post(
                reverse("account_login"),
                {
                    "login": athlete.email,
                    "password": PASSWORD,
                    "next": _claim_url(invite),
                },
            )
            now.return_value = self.NOW + 3600
            resp = client.get(_claim_url(invite))
        assert resp.status_code == 302
        assert CoachAthlete.objects.get(athlete=athlete).is_active

    def test_unrelated_login_clears_the_flag(self, client):
        invite = _invite()
        athlete = _existing_user()
        client.get(_claim_url(invite))
        client.post(
            reverse("account_login"),
            {
                "login": athlete.email,
                "password": PASSWORD,
                "next": reverse("meso:athlete_home"),
            },
        )
        assert "meso_claim_token" not in client.session
        resp = client.get(_claim_url(invite))
        assert resp.status_code == 200
        assert b"Accept invite" in resp.content
        assert not CoachAthlete.objects.filter(athlete=athlete).exists()

    def test_login_without_next_clears_the_flag(self, client):
        invite = _invite()
        athlete = _existing_user()
        client.get(_claim_url(invite))
        client.post(
            reverse("account_login"), {"login": athlete.email, "password": PASSWORD}
        )
        assert "meso_claim_token" not in client.session

    # -- guards (green on main too: the chain must survive the new clearing) --

    def test_login_following_the_claim_still_auto_accepts(self, client):
        invite = _invite()
        athlete = _existing_user()
        client.get(_claim_url(invite))
        resp = client.post(
            reverse("account_login"),
            {"login": athlete.email, "password": PASSWORD, "next": _claim_url(invite)},
        )
        assert resp.url == _claim_url(invite)
        resp = client.get(resp.url)
        assert resp.url == reverse("meso:athlete_home")
        assert CoachAthlete.objects.get(athlete=athlete).is_active

    def test_signup_chain_still_auto_accepts(self, client):
        invite = _invite()
        client.get(_claim_url(invite))
        resp = client.post(
            reverse("account_signup"),
            {
                "email": "jordan.new@example.com",
                "password1": PASSWORD,
                "name": "Jordan Ellis",
                "next": _claim_url(invite),
            },
        )
        assert resp.url == _claim_url(invite)
        assert "meso_claim_token" in client.session
        resp = client.get(resp.url)
        assert resp.url == reverse("meso:athlete_home")
        athlete = User.objects.get(email="jordan.new@example.com")
        assert CoachAthlete.objects.get(athlete=athlete).is_active


# ---------------------------------------------------------------------------
# #671 (2) — no second "accepted" email when the athlete already trains there
# ---------------------------------------------------------------------------


class TestNoDuplicateAcceptedEmail:
    def _claim(self, client, invite, athlete, capture):
        client.force_login(athlete)
        with capture(execute=True):
            return client.post(_claim_url(invite), {"action": "accept"})

    def test_fresh_athlete_still_emails_the_coach(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory(email="sam@example.com")
        invite = _invite(coach)
        self._claim(
            client,
            invite,
            UserFactory(name="Jordan"),
            django_capture_on_commit_callbacks,
        )
        assert len(mail.outbox) == 1

    def test_already_active_athlete_does_not_re_email(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory(email="sam@example.com")
        athlete = UserFactory(name="Jordan")
        CoachAthleteFactory(coach=coach, athlete=athlete, status=ACTIVE)
        invite = _invite(coach)
        self._claim(client, invite, athlete, django_capture_on_commit_callbacks)
        assert mail.outbox == []
        assert CoachAthlete.objects.filter(coach=coach, athlete=athlete).count() == 1


def test_job_aborts_when_link_ends_between_first_check_and_provider_call(monkeypatch):
    """Review P1: client init sits between the first check and propose()."""
    plan, _, _ = make_plan()
    batch = service.create_drafting_batch(
        plan,
        "tweak it",
        coach=plan.relationship.coach,
        mesocycle=plan.mesocycles.first(),
    )
    client = CountingClient()

    def end_then_client():
        plan.relationship.end(by="coach")
        return client

    monkeypatch.setattr(service.client_module, "get_default_client", end_then_client)
    service.run_proposal_job(batch.pk)
    assert client.calls == 0


def test_admin_login_clears_the_claim_flag(rf):
    from django.contrib.auth import login
    from django.contrib.sessions.backends.cache import SessionStore

    staff = UserFactory(is_staff=True)
    request = rf.post("/backside/login/")
    request.session = SessionStore()
    request.session["meso_claim_token"] = "t"
    request.session["meso_claim_at"] = 1.0
    login(request, staff, backend="django.contrib.auth.backends.ModelBackend")
    assert "meso_claim_token" not in request.session
