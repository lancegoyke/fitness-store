"""Phase 3 (spreadsheet parity §3.4) — template plans are first-class.

A template = a ``Plan`` with ``is_template=True``, no relationship (no
athlete), and an ``owner`` — editable in the SAME designer grid. Covers:

- the model: ``str(plan)`` / ``.coach`` are safe with no relationship (the
  2c Codex crash finding), the check constraint blocks a template WITH a
  relationship, and ``is_editable_by`` / ``editable_by`` grant the owner
  (and still deny everyone else);
- the designer: the owner opens + saves a template plan; a non-owner coach
  404s; the deliver surfaces bounce/refuse safely (no athlete to nudge);
- the guard: template plans never appear on athlete surfaces (they are
  relationship-rooted, templates have none).
"""

import json

import pytest
from django.db import IntegrityError
from django.db import transaction
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.serializers import serialize_athlete_identity
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


def template_plan(owner=None, *, title="402", with_grid=True):
    """A template plan (optionally with one block/week/day/cell) for ``owner``."""
    plan = PlanFactory(
        relationship=None,
        is_template=True,
        owner=owner or UserFactory(),
        title=title,
        status=Plan.Status.ACTIVE,
    )
    if not with_grid:
        return plan, None
    meso = MesocycleFactory(plan=plan, name=title, order=0)
    week = WeekFactory(mesocycle=meso, index=1)
    session = day(week, day_number=1, name="Day 1")
    cell = presc(session, name="A) Squat jump", sets="3", reps="3")
    return plan, cell


class TestTemplatePlanModel:
    def test_str_is_safe_without_an_athlete(self):
        plan, _ = template_plan(with_grid=False)
        assert str(plan) == "402 (template)"

    def test_str_is_safe_for_any_relationshipless_plan(self):
        plan = PlanFactory(relationship=None, title="Orphan")
        assert str(plan) == "Orphan"

    def test_coach_is_the_owner(self):
        owner = UserFactory()
        plan, _ = template_plan(owner, with_grid=False)
        assert plan.coach == owner

    def test_coach_still_rides_the_relationship_for_regular_plans(self):
        plan = PlanFactory()
        assert plan.coach == plan.relationship.coach

    def test_check_constraint_blocks_template_with_relationship(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            PlanFactory(relationship=CoachAthleteFactory(), is_template=True)

    def test_is_editable_by_grants_the_owner_and_denies_others(self):
        owner = UserFactory()
        plan, _ = template_plan(owner, with_grid=False)
        assert plan.is_editable_by(owner)
        assert not plan.is_editable_by(UserFactory())

    def test_editable_by_queryset_mirrors_the_owner_grant(self):
        owner = UserFactory()
        plan, _ = template_plan(owner, with_grid=False)
        assert plan in Plan.objects.editable_by(owner)
        assert plan not in Plan.objects.editable_by(UserFactory())

    def test_for_coach_and_for_athlete_never_include_templates(self):
        owner = UserFactory()
        plan, _ = template_plan(owner, with_grid=False)
        assert plan not in Plan.objects.for_coach(owner)
        assert Plan.objects.for_athlete(owner).count() == 0

    def test_identity_serializes_the_template_placeholder(self):
        plan, _ = template_plan(with_grid=False)
        identity = serialize_athlete_identity(plan)
        assert identity["name"] == "402"
        assert identity["goal"] == "Template"
        assert identity["contraindications"] == []


class TestTemplateDesignerAccess:
    def test_owner_opens_the_designer_on_a_template(self, client):
        owner = UserFactory()
        plan, _ = template_plan(owner, title="Base 1")
        client.force_login(owner)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'id="meso-grid-data"' in body
        assert "A) Squat jump" in body

    def test_non_owner_coach_404s(self, client):
        plan, _ = template_plan()
        other_coach = CoachAthleteFactory().coach
        client.force_login(other_coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 404

    def test_owner_saves_a_cell_on_a_template(self, client):
        owner = UserFactory()
        plan, cell = template_plan(owner)
        client.force_login(owner)
        resp = client.post(
            reverse(
                "meso:api_prescription_patch",
                kwargs={"plan_id": plan.pk, "pk": cell.pk},
            ),
            data=json.dumps({"text": "4 x 6, RPE 8"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        cell.refresh_from_db()
        assert cell.text == "4 x 6, RPE 8"

    def test_non_owner_cannot_save(self, client):
        plan, cell = template_plan()
        client.force_login(UserFactory())
        resp = client.post(
            reverse(
                "meso:api_prescription_patch",
                kwargs={"plan_id": plan.pk, "pk": cell.pk},
            ),
            data=json.dumps({"text": "hacked"}),
            content_type="application/json",
        )
        assert resp.status_code == 403


class TestTemplateDeliverSurfaces:
    def test_deliver_screen_bounces_back_to_the_designer(self, client):
        owner = UserFactory()
        plan, _ = template_plan(owner)
        client.force_login(owner)
        resp = client.get(reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})

    def test_deliver_post_refuses_a_template(self, client):
        owner = UserFactory()
        plan, _ = template_plan(owner)
        client.force_login(owner)
        resp = client.post(
            reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})
        )
        assert resp.status_code == 400

    def test_agent_refuses_a_template(self, client):
        owner = UserFactory()
        plan, _ = template_plan(owner)
        client.force_login(owner)
        resp = client.post(
            reverse("meso:api_plan_agent", kwargs={"plan_id": plan.pk}),
            data=json.dumps({"instruction": "lighten Friday"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "athlete" in resp.json()["error"]


class TestTemplatesNeverReachAthleteSurfaces:
    def test_athlete_home_shows_no_template_plans(self, client):
        # Templates are relationship-less; athlete surfaces are relationship-
        # rooted — this should hold with zero template-aware code.
        rel = CoachAthleteFactory()
        template_plan(rel.coach, title="A Template")
        client.force_login(rel.athlete)
        resp = client.get(reverse("meso:athlete_home"))
        assert resp.status_code == 200
        assert "A Template" not in resp.content.decode()
