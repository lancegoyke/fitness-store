"""Guided demo onboarding tour — Phase 4: analytics funnel events (#430).

The ``analytics`` app is Google-Analytics-script-only (a context processor
handing a GA property id to the template — see
``store_project/analytics/context_processors.py``); there's no server-side
event model to reuse, and a client-side GA/JS beacon would fail the plan's
own "can't be ad-blocked away" requirement anyway. So this is a minimal,
meso-local ``TourEvent`` table instead, recorded server-side at the tour's
own endpoints via ``tour.py``'s ``record_*`` helpers. Covers:

- ``TourEvent`` model sanity, including the ``coach`` FK's ``SET_NULL`` (a
  reaped sandbox coach must not silently erase the funnel counts);
- the ``tour.record_*`` helpers persisting the right ``kind``/``variant``/
  ``step_key``/``segment``, and ``record_event`` swallowing a write failure
  rather than raising;
- ``sandbox.create_sandbox`` recording a **started** event (sandbox variant);
- ``meso:tour_state``: advance/goto only record **advanced** when the step
  actually moves forward (not on "back", not on a goto that moves backward,
  not on an already-clamped no-op); dismiss/complete/restart record their own
  events;
- ``meso:tour_skip`` records **skipped** (never **completed**, even though
  both leave ``tour_state`` parked on the same ``"completed"`` status);
- ``demo_load``'s per-segment opt-in records (aggregate load does not);
- ``roster_add_self``/``plan_create`` only record an opt-in when the POST
  carries the tour driver's ``tour=1`` marker — both endpoints are hit
  organically far more often, and an unmarked post must record nothing while
  still performing its real action.
"""

import pytest
from django.urls import reverse

from store_project.meso import sandbox
from store_project.meso import tour
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import TourEvent
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _coach():
    coach = UserFactory()
    CoachProfile.objects.create(user=coach)
    return coach


# ---------------------------------------------------------------------------
# TourEvent model sanity
# ---------------------------------------------------------------------------


class TestTourEventModel:
    def test_str_includes_step_or_segment(self):
        coach = _coach()
        event = TourEvent.objects.create(
            coach=coach,
            kind=TourEvent.Kind.OPT_IN,
            variant=TourEvent.Variant.SANDBOX,
            step_key="welcome",
            segment="athletes",
        )
        assert "athletes" in str(event) or "welcome" in str(event)

    def test_coach_is_set_null_on_delete(self):
        """A reaped sandbox coach must not take the funnel row down with it."""
        coach = _coach()
        event = TourEvent.objects.create(
            coach=coach,
            kind=TourEvent.Kind.STARTED,
            variant=TourEvent.Variant.SANDBOX,
            step_key="welcome",
        )
        coach.delete()
        event.refresh_from_db()
        assert event.coach_id is None


# ---------------------------------------------------------------------------
# tour.py record_* helpers
# ---------------------------------------------------------------------------


class TestRecordHelpers:
    def test_record_started(self):
        coach = _coach()
        tour.record_started(coach, "self")
        event = TourEvent.objects.get()
        assert event.kind == TourEvent.Kind.STARTED
        assert event.variant == "self"
        assert event.step_key == tour.STEPS[0]["key"]
        assert event.coach == coach
        assert event.segment == ""

    def test_record_advanced(self):
        coach = _coach()
        tour.record_advanced(coach, "sandbox", "designer")
        event = TourEvent.objects.get()
        assert event.kind == TourEvent.Kind.ADVANCED
        assert event.variant == "sandbox"
        assert event.step_key == "designer"

    def test_record_dismissed(self):
        coach = _coach()
        tour.record_dismissed(coach, "self", "results")
        event = TourEvent.objects.get()
        assert event.kind == TourEvent.Kind.DISMISSED
        assert event.step_key == "results"

    def test_record_completed(self):
        coach = _coach()
        tour.record_completed(coach, "sandbox")
        event = TourEvent.objects.get()
        assert event.kind == TourEvent.Kind.COMPLETED
        assert event.step_key == tour.STEPS[-1]["key"]

    def test_record_skipped(self):
        coach = _coach()
        tour.record_skipped(coach, "sandbox", "profile")
        event = TourEvent.objects.get()
        assert event.kind == TourEvent.Kind.SKIPPED
        assert event.step_key == "profile"

    def test_record_opt_in(self):
        coach = _coach()
        tour.record_opt_in(coach, "sandbox", "welcome", "athletes")
        event = TourEvent.objects.get()
        assert event.kind == TourEvent.Kind.OPT_IN
        assert event.step_key == "welcome"
        assert event.segment == "athletes"

    def test_step_key_for_segment(self):
        assert tour.step_key_for_segment("athletes") == "welcome"
        assert tour.step_key_for_segment("program") == "designer"
        assert tour.step_key_for_segment("delivery") == "deliver"
        assert tour.step_key_for_segment("log") == "results"
        assert tour.step_key_for_segment("group") == "groups"
        assert tour.step_key_for_segment("bogus") == ""


class TestRecordEventNeverBreaksTheCaller:
    def test_a_write_failure_is_swallowed_not_raised(self, monkeypatch):
        coach = _coach()

        def _boom(**kwargs):
            raise RuntimeError("db is down")

        monkeypatch.setattr(TourEvent.objects, "create", _boom)

        # Must not raise.
        tour.record_event(coach, TourEvent.Kind.STARTED, variant="sandbox")

    def test_a_write_failure_does_not_poison_an_outer_transaction(self, monkeypatch):
        """The savepoint (nested atomic()) is what makes this safe under Postgres.

        Simulates the real shape of a call site like ``plan_create``, which
        wraps its own work in ``transaction.atomic()`` — a raw (un-savepointed)
        DB error inside that block would otherwise abort the *whole* outer
        transaction, not just the analytics insert.
        """
        from django.db import transaction

        coach = _coach()

        def _boom(**kwargs):
            raise RuntimeError("db is down")

        monkeypatch.setattr(TourEvent.objects, "create", _boom)

        with transaction.atomic():
            tour.record_event(coach, TourEvent.Kind.STARTED, variant="sandbox")
            # The outer transaction must still be usable after the swallowed
            # failure — proven by a real write succeeding right after it.
            CoachProfile.objects.filter(pk=coach.coach_profile.pk).update(
                display_name="still alive"
            )

        coach.coach_profile.refresh_from_db()
        assert coach.coach_profile.display_name == "still alive"


# ---------------------------------------------------------------------------
# sandbox.create_sandbox — auto-start
# ---------------------------------------------------------------------------


class TestSandboxStartRecordsEvent:
    def test_create_sandbox_records_a_started_event(self):
        user = sandbox.create_sandbox()

        event = TourEvent.objects.get()
        assert event.kind == TourEvent.Kind.STARTED
        assert event.variant == "sandbox"
        assert event.step_key == "welcome"
        assert event.coach == user


# ---------------------------------------------------------------------------
# meso:tour_state
# ---------------------------------------------------------------------------


class TestTourStateEndpointEvents:
    def test_advance_records_advanced_with_the_new_steps_key(self, client):
        user = sandbox.create_sandbox()
        TourEvent.objects.all().delete()  # drop the "started" row
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "advance"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        event = TourEvent.objects.get(kind=TourEvent.Kind.ADVANCED)
        assert event.step_key == tour.STEPS[1]["key"]
        assert event.variant == "sandbox"

    def test_advance_clamped_at_the_last_step_records_nothing(self, client):
        user = sandbox.create_sandbox()
        profile = CoachProfile.objects.get(user=user)
        tour.set_step(profile, len(tour.STEPS) - 1)
        TourEvent.objects.all().delete()
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "advance"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert not TourEvent.objects.filter(kind=TourEvent.Kind.ADVANCED).exists()

    def test_back_records_nothing(self, client):
        user = sandbox.create_sandbox()
        profile = CoachProfile.objects.get(user=user)
        tour.set_step(profile, 3)
        TourEvent.objects.all().delete()
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "back"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert TourEvent.objects.count() == 0

    def test_goto_forward_records_advanced(self, client):
        user = sandbox.create_sandbox()
        TourEvent.objects.all().delete()
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "goto", "step": "4"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        event = TourEvent.objects.get(kind=TourEvent.Kind.ADVANCED)
        assert event.step_key == tour.STEPS[4]["key"]

    def test_goto_backward_records_nothing(self, client):
        user = sandbox.create_sandbox()
        profile = CoachProfile.objects.get(user=user)
        tour.set_step(profile, 4)
        TourEvent.objects.all().delete()
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "goto", "step": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert TourEvent.objects.count() == 0

    def test_goto_same_step_records_nothing(self, client):
        user = sandbox.create_sandbox()
        profile = CoachProfile.objects.get(user=user)
        tour.set_step(profile, 2)
        TourEvent.objects.all().delete()
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "goto", "step": "2"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert TourEvent.objects.count() == 0

    def test_dismiss_records_dismissed_with_the_current_step(self, client):
        user = sandbox.create_sandbox()
        profile = CoachProfile.objects.get(user=user)
        tour.set_step(profile, 2)
        TourEvent.objects.all().delete()
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "dismiss"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        event = TourEvent.objects.get(kind=TourEvent.Kind.DISMISSED)
        assert event.step_key == tour.STEPS[2]["key"]

    def test_complete_records_completed(self, client):
        user = sandbox.create_sandbox()
        TourEvent.objects.all().delete()
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "complete"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        event = TourEvent.objects.get(kind=TourEvent.Kind.COMPLETED)
        assert event.step_key == tour.STEPS[-1]["key"]

    def test_restart_records_started_with_the_self_variant_for_a_real_coach(
        self, client
    ):
        coach = _coach()
        client.force_login(coach)

        client.post(
            reverse("meso:tour_state"),
            {"action": "restart"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        event = TourEvent.objects.get(kind=TourEvent.Kind.STARTED)
        assert event.variant == "self"
        assert event.step_key == "welcome"

    def test_restart_records_the_sandbox_variant_for_a_sandbox_coach(self, client):
        user = sandbox.create_sandbox()
        tour.complete(CoachProfile.objects.get(user=user))
        TourEvent.objects.all().delete()
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "restart"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        event = TourEvent.objects.get(kind=TourEvent.Kind.STARTED)
        assert event.variant == "sandbox"

    def test_unknown_action_records_nothing(self, client):
        user = sandbox.create_sandbox()
        TourEvent.objects.all().delete()
        client.force_login(user)

        client.post(
            reverse("meso:tour_state"),
            {"action": "bogus"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert TourEvent.objects.count() == 0


# ---------------------------------------------------------------------------
# meso:tour_skip
# ---------------------------------------------------------------------------


class TestTourSkipEndpointEvents:
    def test_skip_records_skipped_not_completed(self, client):
        user = sandbox.create_sandbox()
        profile = CoachProfile.objects.get(user=user)
        tour.set_step(profile, 3)
        TourEvent.objects.all().delete()
        client.force_login(user)

        client.post(reverse("meso:tour_skip"))

        assert TourEvent.objects.filter(kind=TourEvent.Kind.SKIPPED).count() == 1
        assert TourEvent.objects.filter(kind=TourEvent.Kind.COMPLETED).count() == 0
        event = TourEvent.objects.get()
        assert event.step_key == tour.STEPS[3]["key"]
        assert event.variant == "sandbox"


# ---------------------------------------------------------------------------
# demo_load — per-segment opt-in
# ---------------------------------------------------------------------------


class TestDemoLoadSegmentOptIn:
    def test_segment_load_records_opt_in(self, client):
        coach = _coach()
        client.force_login(coach)

        client.post(reverse("meso:demo_load"), {"segment": "program"})

        event = TourEvent.objects.get(kind=TourEvent.Kind.OPT_IN)
        assert event.variant == "sandbox"
        assert event.step_key == "designer"
        assert event.segment == "program"

    def test_aggregate_load_records_nothing(self, client):
        coach = _coach()
        client.force_login(coach)

        client.post(reverse("meso:demo_load"))

        assert TourEvent.objects.count() == 0

    def test_unknown_segment_400s_and_records_nothing(self, client):
        coach = _coach()
        client.force_login(coach)

        resp = client.post(reverse("meso:demo_load"), {"segment": "nope"})

        assert resp.status_code == 400
        assert TourEvent.objects.count() == 0


# ---------------------------------------------------------------------------
# roster_add_self — tour=1-gated opt-in
# ---------------------------------------------------------------------------


class TestRosterAddSelfOptIn:
    def test_tour_marked_post_records_opt_in(self, client):
        coach = _coach()
        client.force_login(coach)

        client.post(reverse("meso:roster_add_self"), {"tour": "1"})

        event = TourEvent.objects.get(kind=TourEvent.Kind.OPT_IN)
        assert event.variant == "self"
        assert event.step_key == "welcome"
        assert event.segment == "roster_add_self"

    def test_organic_post_records_nothing_but_still_adds_the_self_link(self, client):
        coach = _coach()
        client.force_login(coach)

        client.post(reverse("meso:roster_add_self"))

        assert TourEvent.objects.count() == 0
        assert CoachAthlete.objects.filter(coach=coach, athlete=coach).exists()

    def test_event_write_failure_never_blocks_the_real_action(
        self, client, monkeypatch
    ):
        coach = _coach()

        def _boom(**kwargs):
            raise RuntimeError("db is down")

        monkeypatch.setattr(TourEvent.objects, "create", _boom)
        client.force_login(coach)

        resp = client.post(reverse("meso:roster_add_self"), {"tour": "1"})

        assert resp.status_code == 302
        assert CoachAthlete.objects.filter(coach=coach, athlete=coach).exists()


# ---------------------------------------------------------------------------
# plan_create — tour=1-gated opt-in, step key from `draft`
# ---------------------------------------------------------------------------


class TestPlanCreateOptIn:
    def test_tour_marked_post_records_the_designer_step(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)

        client.post(reverse("meso:plan_create", args=[link.athlete_id]), {"tour": "1"})

        event = TourEvent.objects.get(kind=TourEvent.Kind.OPT_IN)
        assert event.variant == "self"
        assert event.step_key == "designer"
        assert event.segment == "plan_create"

    def test_tour_marked_draft_post_records_the_agent_step(self, client, monkeypatch):
        class _FakeDraftClient:
            model = "fake"

            def propose(self, *, context, instruction):
                return {"summary": "drafted", "changes": []}

        from store_project.meso.agent import client as client_module

        link = CoachAthleteFactory()
        monkeypatch.setattr(
            client_module, "get_default_client", lambda: _FakeDraftClient()
        )
        client.force_login(link.coach)

        client.post(
            reverse("meso:plan_create", args=[link.athlete_id]),
            {"tour": "1", "draft": "agent"},
        )

        event = TourEvent.objects.get(kind=TourEvent.Kind.OPT_IN)
        assert event.step_key == "agent"

    def test_organic_post_records_nothing_but_still_creates_the_plan(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)

        client.post(reverse("meso:plan_create", args=[link.athlete_id]))

        assert TourEvent.objects.count() == 0
        assert link.working_plan() is not None
