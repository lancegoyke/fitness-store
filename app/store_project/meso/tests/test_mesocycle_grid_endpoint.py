"""GET /meso/api/plan/<id>/grid/ — the P1 multi-week table's data (backend).

``serialize_mesocycle_grid`` (``test_serializers.py``) builds the dense
day × row × week shape; this endpoint exposes it read-only, scoped by
ownership only (mirrors ``week_view`` — not billing-gated, since viewing never
mutates anything). It defaults to the plan's earliest-live-week mesocycle (the
block that week belongs to) and accepts ``?mesocycle=<id>`` to view another
block in the same plan. The designer view also hydrates the same payload into
the page (``grid_data``) for the initial page load.
"""

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


def _aged_link(coach, days_ago, **kwargs):
    from datetime import timedelta

    from django.utils import timezone

    link = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE, **kwargs)
    CoachAthlete.objects.filter(pk=link.pk).update(
        created_at=timezone.now() - timedelta(days=days_ago)
    )
    link.refresh_from_db()
    return link


def _seeded_plan():
    """An owned plan with a two-day, two-week current block."""
    link = CoachAthleteFactory()
    plan = link.create_plan()  # scaffold: 1 block, 1 week, 2 days, 1 row each
    meso = plan.mesocycles.get()
    meso.append_week()  # a second week, same block-shared days/rows
    return link, plan, meso


class TestMesocycleGridEndpoint:
    def _url(self, plan):
        return reverse("meso:api_mesocycle_grid", kwargs={"plan_id": plan.pk})

    def test_returns_ok_and_the_grid_shape_for_the_owner(self, client):
        link, plan, meso = _seeded_plan()
        client.force_login(link.coach)
        resp = client.get(self._url(plan))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["mesocycle"]["id"] == meso.pk
        assert body["mesocycle"]["plan_id"] == plan.pk
        assert len(body["weeks"]) == 2
        assert len(body["days"]) == 2
        assert "history" in body

    def test_grid_is_dense_across_both_weeks(self, client):
        link, plan, meso = _seeded_plan()
        client.force_login(link.coach)
        resp = client.get(self._url(plan))
        body = resp.json()
        week_ids = {str(w["id"]) for w in body["weeks"]}
        for day_data in body["days"]:
            for row in day_data["rows"]:
                assert set(row["cells"].keys()) == week_ids

    def test_requires_login(self, client):
        link, plan, meso = _seeded_plan()
        resp = client.get(self._url(plan))
        assert resp.status_code in (302, 403)

    def test_foreign_coach_forbidden(self, client):
        link, plan, meso = _seeded_plan()
        client.force_login(UserFactory())  # not this plan's coach
        assert client.get(self._url(plan)).status_code in (403, 404)

    def test_404_for_unknown_plan(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)
        resp = client.get(
            reverse("meso:api_mesocycle_grid", kwargs={"plan_id": 999999})
        )
        assert resp.status_code == 404

    def test_over_limit_coach_can_still_view(self, client):
        # Read access is not billing-gated: a suspended coach keeps read access
        # (mirrors week_view's ``test_over_limit_coach_can_still_view``).
        coach = UserFactory()
        _aged_link(coach, days_ago=30)  # kept (oldest)
        suspended = _aged_link(coach, days_ago=1)
        plan = suspended.create_plan()
        client.force_login(coach)
        assert client.get(self._url(plan)).status_code == 200

    def test_rejects_post(self, client):
        link, plan, meso = _seeded_plan()
        client.force_login(link.coach)
        assert client.post(self._url(plan)).status_code == 405

    def test_mesocycle_query_param_selects_that_block(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        first_meso = plan.mesocycles.get()
        second_meso = MesocycleFactory(plan=plan, name="Block 2", order=1)
        week = WeekFactory(mesocycle=second_meso, index=1)
        session = day(week, day_number=1, name="Only day")
        presc(session, name="Only row")
        client.force_login(link.coach)
        resp = client.get(self._url(plan) + f"?mesocycle={second_meso.pk}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mesocycle"]["id"] == second_meso.pk
        assert body["mesocycle"]["id"] != first_meso.pk

    def test_mesocycle_query_param_rejects_a_foreign_mesocycle(self, client):
        link, plan, meso = _seeded_plan()
        other_meso = MesocycleFactory()  # belongs to a different plan
        client.force_login(link.coach)
        resp = client.get(self._url(plan) + f"?mesocycle={other_meso.pk}")
        assert resp.status_code == 404

    def test_mesocycle_query_param_rejects_a_nonexistent_id(self, client):
        link, plan, meso = _seeded_plan()
        client.force_login(link.coach)
        resp = client.get(self._url(plan) + "?mesocycle=999999")
        assert resp.status_code == 404

    def test_mesocycle_query_param_rejects_a_non_integer(self, client):
        link, plan, meso = _seeded_plan()
        client.force_login(link.coach)
        resp = client.get(self._url(plan) + "?mesocycle=not-an-int")
        assert resp.status_code == 400

    def test_defaults_to_the_earliest_live_weeks_mesocycle(self, client):
        """No ``?mesocycle=`` defaults to the plan's earliest live week's block.

        There used to be a per-plan "current" flag a coach could set on any
        week to override plain ordering; that pointer is gone (see
        ``docs/meso/remove-current-week-plan.md``) — the default is now purely
        the earliest ``(mesocycle.order, index)`` live week's block, so a
        later block never wins by default.
        """
        link = CoachAthleteFactory()
        plan = link.create_plan()
        first_meso = plan.mesocycles.get()
        second_meso = MesocycleFactory(plan=plan, name="Block 2", order=1)
        WeekFactory(mesocycle=second_meso, index=1)
        client.force_login(link.coach)
        resp = client.get(self._url(plan))
        assert resp.json()["mesocycle"]["id"] == first_meso.pk

    def test_no_mesocycle_at_all_is_404(self, client):
        rel = CoachAthleteFactory()
        plan = PlanFactory(relationship=rel)  # bare — no scaffold, no mesocycle
        client.force_login(rel.coach)
        resp = client.get(self._url(plan))
        assert resp.status_code == 404

    def test_mesocycle_with_no_weeks_returns_an_empty_ish_grid(self, client):
        rel = CoachAthleteFactory()
        plan = PlanFactory(relationship=rel)
        MesocycleFactory(plan=plan, name="Empty block", order=0)
        client.force_login(rel.coach)
        resp = client.get(self._url(plan))
        assert resp.status_code == 200
        body = resp.json()
        assert body["weeks"] == []
        assert body["days"] == []


class TestDesignerViewGridContext:
    def test_designer_context_includes_grid_data(self, client):
        link, plan, meso = _seeded_plan()
        client.force_login(link.coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        grid_data = resp.context["grid_data"]
        assert grid_data["mesocycle"]["id"] == meso.pk
        assert len(grid_data["weeks"]) == 2
        body = resp.content.decode()
        assert 'id="meso-grid-data"' in body
