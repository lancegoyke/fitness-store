"""First-time UX — Phase 1: individual plan creation (the structural fix).

Until now the only thing that built an individual ``Plan → Mesocycle → Week →
Session → ExercisePrescription`` tree was the ``seed_meso_demo`` command — there
was no UI path, so a real (non-seeded) coach could not build an individual
program at all. Both individual-plan CTAs ("+ New program", "Build a program")
bounced off the bare designer back to the roster. This slice makes the core verb
real:

- ``Plan.scaffold`` — the minimal-but-usable starter tree, shared by individual +
  group plan creation (``MesoGroup.create_shared_plan`` is refactored onto it).
- ``CoachAthlete.create_plan`` / ``CoachAthlete.working_plan`` — create (or find)
  an individual program rooted at the relationship.
- ``plan_create`` (``POST /meso/athlete/<uuid>/plan/new/``) — coach-scoped,
  billing-gated (a suspended athlete is frozen, D6), reuse-or-create, lands in
  the designer.
- ``session_add`` (``POST /meso/api/plan/<id>/session/``) — append a training day
  to the current week so the scaffold can grow.
- the create → edit → deliver → athlete-sees-it round trip works **without the
  seed**.
"""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.billing import access
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.meso.models import Session
from store_project.meso.models import Unit
from store_project.meso.models import Week
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _aged_link(coach, days_ago, **kwargs):
    """An active ``CoachAthlete`` for ``coach`` whose ``created_at`` is back-dated.

    ``created_at`` is ``auto_now_add``, so a raw ``.update()`` is the only way to
    set a deterministic relationship age — the ordering the oldest-kept
    suspension rule turns on.
    """
    link = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE, **kwargs)
    CoachAthlete.objects.filter(pk=link.pk).update(
        created_at=timezone.now() - timedelta(days=days_ago)
    )
    link.refresh_from_db()
    return link


def _plan_new_url(athlete):
    return reverse("meso:plan_create", kwargs={"pk": athlete.pk})


# ---------------------------------------------------------------------------
# Plan.scaffold + CoachAthlete.create_plan / working_plan  (the model layer)
# ---------------------------------------------------------------------------


class TestScaffoldAndCreatePlan:
    def test_create_plan_builds_a_usable_scaffold(self):
        link = CoachAthleteFactory()
        plan = link.create_plan()

        assert plan.relationship_id == link.pk
        assert plan.status == Plan.Status.DRAFT
        # One block, one current week, two training days, one starter row each.
        mesos = list(plan.mesocycles.all())
        assert len(mesos) == 1
        weeks = list(mesos[0].weeks.all())
        assert len(weeks) == 1
        assert weeks[0].is_current is True
        sessions = list(weeks[0].sessions.all())
        assert len(sessions) == 2
        for session in sessions:
            assert session.cells().count() == 1

    def test_create_plan_is_editable_and_deliverable(self):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        # The coach owns it (designer gate) and it has a current week to deliver.
        assert plan.is_editable_by(link.coach) is True
        from store_project.meso.serializers import current_week

        assert current_week(plan) is not None

    def test_create_plan_uses_coach_default_unit(self):
        profile = CoachProfileFactory(default_unit=Unit.POUNDS)
        link = CoachAthleteFactory(coach=profile.user)
        plan = link.create_plan()
        assert plan.unit == Unit.POUNDS

    def test_create_plan_unit_falls_back_without_profile(self):
        link = CoachAthleteFactory()  # coach has no CoachProfile
        plan = link.create_plan()
        assert plan.unit == Unit.KILOGRAMS

    def test_working_plan_none_then_latest(self):
        link = CoachAthleteFactory()
        assert link.working_plan() is None
        plan = link.create_plan()
        assert link.working_plan() == plan

    def test_working_plan_excludes_archived(self):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        plan.status = Plan.Status.ARCHIVED
        plan.save(update_fields=["status"])
        assert link.working_plan() is None


# ---------------------------------------------------------------------------
# plan_create endpoint — coach-scoped, billing-gated, reuse-or-create
# ---------------------------------------------------------------------------


class TestPlanCreateEndpoint:
    def test_creates_plan_and_redirects_to_designer(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)
        resp = client.post(_plan_new_url(link.athlete))
        plan = Plan.objects.get(relationship=link)
        assert resp.status_code == 302
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})
        # The created plan is a real, scaffolded tree.
        assert plan.mesocycles.count() == 1

    def test_is_idempotent_reuses_existing_plan(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)
        first = client.post(_plan_new_url(link.athlete))
        second = client.post(_plan_new_url(link.athlete))
        assert Plan.objects.filter(relationship=link).count() == 1
        assert first.url == second.url

    def test_requires_active_link(self, client):
        # A pending (not active) relationship is not yet a coachable athlete.
        link = CoachAthleteFactory(status=CoachAthlete.Status.PENDING_ATHLETE_REQUEST)
        client.force_login(link.coach)
        resp = client.post(_plan_new_url(link.athlete))
        assert resp.status_code == 404
        assert not Plan.objects.filter(relationship=link).exists()

    def test_foreign_athlete_is_404(self, client):
        coach = UserFactory()
        other = CoachAthleteFactory()  # someone else's athlete
        client.force_login(coach)
        resp = client.post(_plan_new_url(other.athlete))
        assert resp.status_code == 404
        assert not Plan.objects.filter(relationship=other).exists()

    def test_requires_login(self, client):
        link = CoachAthleteFactory()
        resp = client.post(_plan_new_url(link.athlete))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_rejects_get(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)
        resp = client.get(_plan_new_url(link.athlete))
        assert resp.status_code == 405

    def test_blocked_for_suspended_athlete(self, client):
        coach = UserFactory()  # free tier, cap 1
        _aged_link(coach, days_ago=30)  # oldest → kept
        suspended = _aged_link(coach, days_ago=1)  # newest → over cap → suspended
        assert suspended.pk in access.suspended_athlete_ids(coach)
        client.force_login(coach)
        resp = client.post(_plan_new_url(suspended.athlete))
        # Frozen: a flashed redirect back to the athlete, no plan created.
        assert resp.status_code == 302
        assert resp.url == reverse("meso:athlete", kwargs={"pk": suspended.athlete_id})
        assert not Plan.objects.filter(relationship=suspended).exists()

    def test_over_limit_can_create_for_kept_athlete(self, client):
        coach = UserFactory()
        kept = _aged_link(coach, days_ago=30)  # oldest → kept editable
        _aged_link(coach, days_ago=1)  # pushes coach over the cap
        assert access.is_over_limit(coach) is True
        client.force_login(coach)
        resp = client.post(_plan_new_url(kept.athlete))
        assert resp.status_code == 302
        assert Plan.objects.filter(relationship=kept).exists()


# ---------------------------------------------------------------------------
# session_add endpoint — grow the scaffold (add a training day)
# ---------------------------------------------------------------------------


class TestSessionAddEndpoint:
    def _url(self, plan):
        return reverse("meso:api_session_add", kwargs={"plan_id": plan.pk})

    def test_appends_a_day_with_a_starter_row(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        week = Week.objects.get(mesocycle__plan=plan)
        before = week.sessions.count()
        client.force_login(link.coach)
        resp = client.post(self._url(plan))
        assert resp.status_code == 201
        body = resp.json()
        assert body["ok"] is True
        assert week.sessions.count() == before + 1
        new_session = Session.objects.get(pk=body["session"]["id"])
        assert new_session.week_id == week.pk
        assert new_session.cells().count() == 1
        # Returned in the designer's day shape so the grid can append it.
        assert set(body["session"]) >= {"id", "n", "name", "exercises"}

    def test_bumps_plan_modified(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        before = plan.modified
        client.force_login(link.coach)
        client.post(self._url(plan))
        plan.refresh_from_db()
        assert plan.modified > before

    def test_foreign_coach_forbidden(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(UserFactory())  # not this plan's coach
        resp = client.post(self._url(plan))
        assert resp.status_code in (403, 404)
        assert Session.objects.filter(week__mesocycle__plan=plan).count() == 2

    def test_over_limit_suspended_plan_is_402(self, client):
        coach = UserFactory()
        _aged_link(coach, days_ago=30)  # kept, pushes nothing
        suspended = _aged_link(coach, days_ago=1)
        plan = suspended.create_plan()
        client.force_login(coach)
        resp = client.post(self._url(plan))
        assert resp.status_code == 402
        assert resp.json()["over_limit"] is True

    def test_rejects_get(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)
        assert client.get(self._url(plan)).status_code == 405


# ---------------------------------------------------------------------------
# CTA wiring — the dead-end links now POST to plan_create
# (source/render-level, per the test_designer_agent_chat.py precedent)
# ---------------------------------------------------------------------------


class TestCtaWiring:
    def test_roster_offers_new_program_per_athlete(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        # A form posting to plan_create for the coach's athlete is present.
        assert _plan_new_url(link.athlete).encode() in resp.content

    def test_athlete_profile_build_program_posts_to_create(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)
        resp = client.get(reverse("meso:athlete", kwargs={"pk": link.athlete_id}))
        assert resp.status_code == 200
        assert _plan_new_url(link.athlete).encode() in resp.content

    def test_athlete_profile_links_existing_plan_to_designer(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)
        resp = client.get(reverse("meso:athlete", kwargs={"pk": link.athlete_id}))
        designer_url = reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})
        assert designer_url.encode() in resp.content


# ---------------------------------------------------------------------------
# The headline "Done when": create → edit → deliver → athlete sees it, no seed
# ---------------------------------------------------------------------------


class TestFreshCoachRoundTrip:
    def test_create_edit_deliver_athlete_sees_it(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthleteFactory(
            coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )

        # 1) Coach creates an individual program from the UI.
        client.force_login(coach)
        create = client.post(_plan_new_url(athlete))
        assert create.status_code == 302
        plan = Plan.objects.get(relationship=link)

        # 2) Coach edits it — autosave one of the scaffold rows.
        presc = plan.mesocycles.get().weeks.get().sessions.first().cells().first()
        patch = client.post(
            reverse(
                "meso:api_prescription_patch",
                kwargs={"plan_id": plan.pk, "pk": presc.pk},
            ),
            data=json.dumps({"name": "Back Squat", "sets": "5"}),
            content_type="application/json",
        )
        assert patch.status_code == 200

        # 3) Coach delivers the current week.
        deliver = client.post(
            reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})
        )
        assert deliver.status_code == 201

        # 4) The athlete sees the delivered program on their home — no seed used.
        client.force_login(athlete)
        home = client.get(reverse("meso:athlete_home"))
        assert home.status_code == 200
        cards = home.context["plans"]
        card = next(c for c in cards if c["id"] == plan.pk)
        assert card["awaiting"] is False


def test_no_subscription_row_is_free_tier():
    """Sanity: a brand-new coach (no CoachSubscription) is free-tier, cap 1."""
    coach = UserFactory()
    assert not CoachSubscription.objects.filter(coach=coach).exists()
    first = _aged_link(coach, days_ago=10)  # oldest → kept
    # One athlete is within the free cap; a newer second would be suspended.
    assert access.suspended_athlete_ids(coach) == frozenset()
    second = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
    assert second.pk in access.suspended_athlete_ids(coach)
    assert first.pk not in access.suspended_athlete_ids(coach)
