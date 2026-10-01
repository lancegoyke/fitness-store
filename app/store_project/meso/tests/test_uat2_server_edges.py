"""UAT round 2 server edges: #673, #674, #675, #677, #678, #679.

Each class names its issue. The Postgres-only races for #673/#674/#679 live in
``test_sandbox_cap_postgres.py``, ``test_sandbox_reap_race_postgres.py`` and
``test_decline_race_postgres.py``.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import login
from django.contrib.sessions.backends.cache import SessionStore
from django.urls import reverse
from django.utils import timezone

from store_project.meso import demo
from store_project.meso import sandbox
from store_project.meso import tour
from store_project.meso.claim_session import CLAIM_AT_SESSION_KEY
from store_project.meso.claim_session import CLAIM_SESSION_KEY
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import InvalidTransition
from store_project.meso.models import Plan
from store_project.meso.models import SandboxSession
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db

ACTIVE = CoachAthlete.Status.ACTIVE
WAITING = CoachAthlete.Status.ACCEPTED_WAITING


# ---------------------------------------------------------------------------
# #673 — the global sandbox cap is enforced under the creation lock
# ---------------------------------------------------------------------------


class TestSandboxCapIsAuthoritativeAtCreation:
    def test_create_sandbox_refuses_at_the_cap(self, settings):
        settings.MESO_SANDBOX_MAX_CONCURRENT = 1
        sandbox.create_sandbox()
        users_before = User.objects.count()

        with pytest.raises(sandbox.SandboxBusy):
            sandbox.create_sandbox()

        assert SandboxSession.objects.count() == 1
        assert User.objects.count() == users_before

    def test_entry_flashes_busy_when_the_unlocked_pre_check_was_stale(
        self, client, settings, monkeypatch
    ):
        # Two entries both pass the cheap unlocked pre-check, then one loses
        # under the lock: simulate by making the pre-check lie.
        settings.MESO_SANDBOX_MAX_CONCURRENT = 1
        sandbox.create_sandbox()
        real = sandbox.at_capacity
        calls = []

        def lying_once():
            calls.append(1)
            return False if len(calls) == 1 else real()

        monkeypatch.setattr(sandbox, "at_capacity", lying_once)

        resp = client.get(reverse("meso:sandbox_enter"), REMOTE_ADDR="10.9.9.9")

        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert SandboxSession.objects.count() == 1
        assert len(calls) == 2  # the pre-check passed; the lock re-check refused
        assert "_auth_user_id" not in client.session
        msgs = [str(m) for m in resp.wsgi_request._messages]
        assert any("demo is busy" in m for m in msgs)


# ---------------------------------------------------------------------------
# #674 — the reaper re-clears demo athletes inside the coach-delete transaction
# ---------------------------------------------------------------------------


class TestReapSurvivesAMidReapDemoLoad:
    def test_athletes_loaded_between_the_clear_and_the_delete_do_not_orphan(
        self, monkeypatch
    ):
        coach = sandbox.create_sandbox()
        SandboxSession.objects.filter(user=coach).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        emails = [demo.demo_email(coach, spec["slug"]) for spec in demo.ATHLETES]
        assert User.objects.filter(email__in=emails).count() == len(emails)

        real_clear = demo.clear_demo
        calls = []

        def clear_then_reload_once(target):
            real_clear(target)
            if not calls:
                # The still-logged-in sandbox's demo_load lands after the first
                # clear and before the coach delete.
                demo.load_athletes(target)
            calls.append(target.pk)

        monkeypatch.setattr(demo, "clear_demo", clear_then_reload_once)

        assert sandbox.expire_sandboxes() == 1

        assert not User.objects.filter(pk=coach.pk).exists()
        assert User.objects.filter(email__in=emails).count() == 0

    def test_a_sandbox_reaped_elsewhere_meanwhile_is_skipped(self, monkeypatch):
        coach = sandbox.create_sandbox()
        SandboxSession.objects.filter(user=coach).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        real_clear = demo.clear_demo

        def clear_then_vanish(target):
            real_clear(target)
            User.objects.filter(pk=target.pk).delete()
            monkeypatch.setattr(demo, "clear_demo", real_clear)

        monkeypatch.setattr(demo, "clear_demo", clear_then_vanish)

        assert sandbox.expire_sandboxes() == 0


# ---------------------------------------------------------------------------
# #675 — the tour's loaded-ness ignores archived demo data
# ---------------------------------------------------------------------------


class TestTourLoadedIgnoresArchivedDemo:
    def _loaded_flags(self, coach):
        config = tour.build_config(coach, "sandbox")
        return {s["key"]: s["loaded"] for s in config["steps"]}

    def _segment_flags(self, coach):
        by_segment = {}
        for step in tour.build_config(coach, "sandbox")["steps"]:
            segment = next(s for s in tour.STEPS if s["key"] == step["key"])[
                "sandbox"
            ].get("segment")
            if segment:
                by_segment[segment] = step["loaded"]
        return by_segment

    def test_archived_demo_plans_read_as_not_loaded(self):
        coach = sandbox.create_sandbox()
        assert self._segment_flags(coach) == {
            "athletes": True,
            "program": True,
            "delivery": True,
            "log": True,
        }

        Plan.objects.filter(relationship__coach=coach).update(
            status=Plan.Status.ARCHIVED
        )

        flags = self._segment_flags(coach)
        assert flags["athletes"] is True  # the roster is still there
        assert flags["program"] is False
        assert flags["delivery"] is False
        assert flags["log"] is False

    def test_the_remove_demo_predicates_still_see_archived_data(self):
        coach = sandbox.create_sandbox()
        Plan.objects.filter(relationship__coach=coach).update(
            status=Plan.Status.ARCHIVED
        )
        assert demo.has_demo(coach)
        assert demo.has_program(coach)
        assert demo.has_delivery(coach)
        assert demo.has_log(coach)


# ---------------------------------------------------------------------------
# #677 — the claim flag dies on any django-only login
# ---------------------------------------------------------------------------


def _login_with_flag(rf, path, *, user=None, data=None):
    user = user or UserFactory(is_staff=True)
    request = rf.post(path, data or {})
    request.session = SessionStore()
    request.session[CLAIM_SESSION_KEY] = "11111111-1111-1111-1111-111111111111"
    request.session[CLAIM_AT_SESSION_KEY] = 1.0
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return request


class TestClaimFlagClearedOnNonAllauthLogin:
    def test_a_login_outside_the_admin_path_clears_the_flag(self, rf):
        request = _login_with_flag(rf, "/some/where/")
        assert CLAIM_SESSION_KEY not in request.session
        assert CLAIM_AT_SESSION_KEY not in request.session

    def test_the_sandbox_entry_login_clears_the_flag(self, rf):
        request = _login_with_flag(rf, "/meso/demo/")
        assert CLAIM_SESSION_KEY not in request.session

    def test_an_allauth_login_is_left_to_allauths_own_receiver(self, rf):
        request = _login_with_flag(rf, reverse("account_login"))
        assert CLAIM_SESSION_KEY in request.session

    def test_a_social_callback_is_left_to_allauths_own_receiver(self, rf):
        request = _login_with_flag(rf, "/accounts/google/login/callback/")
        assert CLAIM_SESSION_KEY in request.session

    def test_a_login_heading_back_to_the_claim_page_keeps_the_flag(self, rf):
        claim = reverse(
            "meso:invite_claim",
            kwargs={"token": "11111111-1111-1111-1111-111111111111"},
        )
        request = _login_with_flag(rf, "/some/where/", data={"next": claim})
        assert CLAIM_SESSION_KEY in request.session


# ---------------------------------------------------------------------------
# #678 — a seat opening by cascade delete activates the waiting athlete
# ---------------------------------------------------------------------------


class TestCascadeDeleteOpensASeat:
    def test_deleting_an_active_athlete_user_activates_the_waiter(
        self, django_capture_on_commit_callbacks
    ):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        active = CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=ACTIVE)
        waiter = CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=WAITING)

        with django_capture_on_commit_callbacks(execute=True):
            active.athlete.delete()

        waiter.refresh_from_db()
        assert waiter.status == ACTIVE

    def test_deleting_a_non_active_link_schedules_nothing(
        self, django_capture_on_commit_callbacks
    ):
        coach = UserFactory()
        waiter = CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=WAITING)
        other = CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=WAITING)

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            other.athlete.delete()

        assert callbacks == []
        waiter.refresh_from_db()
        assert waiter.status == WAITING

    def test_deleting_the_coach_does_not_blow_up(
        self, django_capture_on_commit_callbacks
    ):
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=ACTIVE)

        with django_capture_on_commit_callbacks(execute=True):
            coach.delete()

        assert not CoachAthlete.objects.exists()

    def test_clearing_the_demo_opens_a_seat_for_a_waiter(
        self, django_capture_on_commit_callbacks
    ):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        demo.load_athletes(coach)
        waiter = CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=WAITING)

        with django_capture_on_commit_callbacks(execute=True):
            demo.clear_demo(coach)

        waiter.refresh_from_db()
        assert waiter.status == ACTIVE


# ---------------------------------------------------------------------------
# #679 — decline re-reads the row under a lock
# ---------------------------------------------------------------------------


class TestDeclineRechecksTheDatabase:
    def test_a_stale_waiting_instance_cannot_decline_a_now_active_link(self):
        link = CoachAthleteFactory(
            coach=UserFactory(), athlete=UserFactory(), status=WAITING
        )
        stale = CoachAthlete.objects.get(pk=link.pk)
        # What activate_waiting does, from another request.
        CoachAthlete.objects.filter(pk=link.pk).update(status=ACTIVE)

        with pytest.raises(InvalidTransition):
            stale.decline()

        link.refresh_from_db()
        assert link.status == ACTIVE

    def test_decline_still_works_and_syncs_the_instance(self):
        link = CoachAthleteFactory(
            coach=UserFactory(), athlete=UserFactory(), status=WAITING
        )
        link.decline()
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.DECLINED


@pytest.mark.django_db
class TestDeclineViewsSurviveALostRace:
    def test_invite_decline_and_withdraw_flash_instead_of_500(
        self, client, monkeypatch
    ):
        def lost(self):
            raise InvalidTransition("raced")

        monkeypatch.setattr(CoachAthlete, "decline", lost)
        coach, athlete = UserFactory(), UserFactory()
        link = CoachAthleteFactory(coach=coach, athlete=athlete, status=WAITING)
        client.force_login(coach)
        resp = client.post(reverse("meso:invite_decline", kwargs={"token": link.token}))
        assert resp.status_code == 302

        pending = CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(),
            status=CoachAthlete.Status.PENDING_ATHLETE_REQUEST,
        )
        client.force_login(pending.athlete)
        resp = client.post(
            reverse("meso:request_withdraw", kwargs={"token": pending.token})
        )
        assert resp.status_code == 302
