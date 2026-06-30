"""Multi-week designer — add weeks, view any week, set the deliver target.

Until now a plan was effectively single-week: ``Plan.scaffold`` materialized one
``Week`` (``is_current``) and the only growth verb was ``session_add`` (a day in
*that* week). A coach could not build a multi-week mesocycle, review an earlier
week, or aim delivery at a week other than the scaffold's first. This slice closes
that long-deferred gap:

- ``Mesocycle.append_week`` — materialize the next week, copying the latest week's
  session/prescription structure (a real progression starting point, not a blank).
- ``GET  /meso/api/plan/<id>/week/<week_id>/``          — view/edit any week (read).
- ``POST /meso/api/plan/<id>/week/``                    — add the next week (write).
- ``POST /meso/api/plan/<id>/week/<week_id>/current/``  — set the deliver target.
- ``serialize_week`` gains ``id``/``index``; ``serialize_plan`` gains ``viewing``
  (the open week's id) so the client tracks which week's grid it is showing.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import LoadType
from store_project.meso.models import Week
from store_project.meso.serializers import serialize_week
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _aged_link(coach, days_ago, **kwargs):
    """An active ``CoachAthlete`` for ``coach`` whose ``created_at`` is back-dated.

    ``created_at`` is ``auto_now_add``, so a raw ``.update()`` is the only way to
    set a deterministic relationship age — the ordering the oldest-kept suspension
    rule turns on (mirrors ``test_plan_create``).
    """
    link = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE, **kwargs)
    CoachAthlete.objects.filter(pk=link.pk).update(
        created_at=timezone.now() - timedelta(days=days_ago)
    )
    link.refresh_from_db()
    return link


def _latest_week(plan):
    return Week.objects.filter(mesocycle__plan=plan).order_by("-index").first()


# ---------------------------------------------------------------------------
# Mesocycle.append_week  (the model layer)
# ---------------------------------------------------------------------------


class TestAppendWeek:
    def test_copies_latest_weeks_structure(self):
        link = CoachAthleteFactory()
        plan = link.create_plan()  # scaffold: 1 block, 1 week, 2 days, 1 row each
        meso = plan.mesocycles.get()
        source = meso.weeks.get()
        # Distinguish the source so the copy is unambiguous.
        source.phase = "Accum"
        source.volume = 88
        source.is_deload = False
        source.save()
        first_day = source.sessions.order_by("order").first()
        first_row = first_day.prescriptions.first()
        first_row.name = "Back Squat"
        first_row.load = "100"
        first_row.load_type = LoadType.PERCENT
        first_row.tags = ["main"]
        first_row.save()

        new_week = meso.append_week()

        assert new_week.index == 2
        assert new_week.is_current is False
        assert new_week.delivered_at is None
        # Meta carried forward as a starting point.
        assert new_week.phase == "Accum"
        assert new_week.volume == 88
        # Same day structure (count + names + day numbers), fresh rows.
        src_days = list(source.sessions.order_by("order"))
        new_days = list(new_week.sessions.order_by("order"))
        assert [d.name for d in new_days] == [d.name for d in src_days]
        assert [d.day_number for d in new_days] == [d.day_number for d in src_days]
        # The copied prescription mirrors the source's fields but is a new row.
        copied = new_days[0].prescriptions.first()
        assert copied.pk != first_row.pk
        assert copied.name == "Back Squat"
        assert copied.load == "100"
        assert copied.load_type == LoadType.PERCENT
        assert copied.tags == ["main"]

    def test_new_week_is_not_current_or_delivered(self):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso = plan.mesocycles.get()
        meso.append_week()
        # The scaffold's first week stays the live one; the new week is a draft.
        assert meso.weeks.filter(is_current=True).count() == 1
        assert meso.weeks.get(index=1).is_current is True
        assert meso.weeks.filter(delivered_at__isnull=False).count() == 0

    def test_grows_week_count_to_track_materialized_weeks(self):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso = plan.mesocycles.get()
        assert meso.week_count == 4  # scaffold default; 1 week materialized
        meso.append_week()  # index 2 — within the planned count
        meso.refresh_from_db()
        assert meso.week_count == 4
        for _ in range(3):  # → indexes 3, 4, 5
            meso.append_week()
        meso.refresh_from_db()
        assert meso.weeks.count() == 5
        assert meso.week_count == 5  # grew past the planned length

    def test_seeds_a_starter_when_block_has_no_weeks(self):
        # A degenerate block (no weeks) still yields an editable week.
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso = plan.mesocycles.get()
        meso.weeks.all().delete()
        new_week = meso.append_week()
        assert new_week.index == 1
        assert new_week.sessions.count() == 1
        assert new_week.sessions.get().prescriptions.count() == 1


# ---------------------------------------------------------------------------
# serialize_week / serialize_plan shape
# ---------------------------------------------------------------------------


class TestSerializeShape:
    def test_serialize_week_includes_id_and_index(self):
        week = WeekFactory(index=3)
        data = serialize_week(week)
        assert data["id"] == week.pk
        assert data["index"] == 3
        assert data["label"] == "Wk 3"

    def test_serialize_plan_reports_the_open_week(self):
        from store_project.meso.serializers import serialize_plan

        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso = plan.mesocycles.get()
        new_week = meso.append_week()
        # Default opens to the current (scaffold) week.
        assert serialize_plan(plan)["viewing"] == meso.weeks.get(index=1).pk
        # Pinned to a week reports that week as open.
        assert serialize_plan(plan, week=new_week)["viewing"] == new_week.pk


# ---------------------------------------------------------------------------
# POST /meso/api/plan/<id>/week/  — add the next week
# ---------------------------------------------------------------------------


class TestWeekAddEndpoint:
    def _url(self, plan):
        return reverse("meso:api_week_add", kwargs={"plan_id": plan.pk})

    def test_adds_a_week_and_returns_the_plan_pinned_to_it(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)
        resp = client.post(self._url(plan))
        assert resp.status_code == 201
        body = resp.json()
        assert body["ok"] is True
        assert Week.objects.filter(mesocycle__plan=plan).count() == 2
        new_week = _latest_week(plan)
        # The response opens onto the new week so the client switches to it.
        assert body["viewing"] == new_week.pk
        assert len(body["weeks"]) == 2
        # New week mirrors the scaffold's two days.
        assert len(body["program"]) == 2

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
        assert Week.objects.filter(mesocycle__plan=plan).count() == 1

    def test_over_limit_suspended_plan_is_402(self, client):
        coach = UserFactory()
        _aged_link(coach, days_ago=30)  # kept (oldest)
        suspended = _aged_link(coach, days_ago=1)
        plan = suspended.create_plan()
        client.force_login(coach)
        resp = client.post(self._url(plan))
        assert resp.status_code == 402
        assert resp.json()["over_limit"] is True
        assert Week.objects.filter(mesocycle__plan=plan).count() == 1

    def test_requires_login(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        resp = client.post(self._url(plan))
        assert resp.status_code in (302, 403)

    def test_rejects_get(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)
        assert client.get(self._url(plan)).status_code == 405


# ---------------------------------------------------------------------------
# GET /meso/api/plan/<id>/week/<week_id>/  — view any week
# ---------------------------------------------------------------------------


class TestWeekViewEndpoint:
    def _url(self, plan, week):
        return reverse(
            "meso:api_week_view", kwargs={"plan_id": plan.pk, "week_id": week.pk}
        )

    def _two_week_plan(self):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso = plan.mesocycles.get()
        week1 = meso.weeks.get(index=1)
        # Rename week 1's first day so we can tell the weeks apart in the program.
        day = week1.sessions.order_by("order").first()
        day.name = "Week-One Lower"
        day.save()
        week2 = meso.append_week()
        day2 = week2.sessions.order_by("order").first()
        day2.name = "Week-Two Lower"
        day2.save()
        return link, plan, week1, week2

    def test_returns_the_target_weeks_program(self, client):
        link, plan, week1, week2 = self._two_week_plan()
        client.force_login(link.coach)
        resp = client.get(self._url(plan, week2))
        assert resp.status_code == 200
        body = resp.json()
        assert body["viewing"] == week2.pk
        assert "Week-Two Lower" in [d["name"] for d in body["program"]]
        assert "Week-One Lower" not in [d["name"] for d in body["program"]]
        assert len(body["weeks"]) == 2

    def test_view_does_not_change_the_current_week(self, client):
        link, plan, week1, week2 = self._two_week_plan()
        client.force_login(link.coach)
        client.get(self._url(plan, week2))
        week1.refresh_from_db()
        week2.refresh_from_db()
        assert week1.is_current is True
        assert week2.is_current is False

    def test_over_limit_coach_can_still_view(self, client):
        # Read access is not billing-gated: a suspended coach keeps read access.
        coach = UserFactory()
        _aged_link(coach, days_ago=30)
        suspended = _aged_link(coach, days_ago=1)
        plan = suspended.create_plan()
        week = Week.objects.get(mesocycle__plan=plan)
        client.force_login(coach)
        assert client.get(self._url(plan, week)).status_code == 200

    def test_foreign_coach_forbidden(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        week = Week.objects.get(mesocycle__plan=plan)
        client.force_login(UserFactory())
        assert client.get(self._url(plan, week)).status_code in (403, 404)

    def test_404_for_a_week_in_another_plan(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        other_week = WeekFactory()  # belongs to a different plan
        client.force_login(link.coach)
        assert client.get(self._url(plan, other_week)).status_code == 404

    def test_requires_login(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        week = Week.objects.get(mesocycle__plan=plan)
        resp = client.get(self._url(plan, week))
        assert resp.status_code in (302, 403)


# ---------------------------------------------------------------------------
# POST /meso/api/plan/<id>/week/<week_id>/current/  — set the deliver target
# ---------------------------------------------------------------------------


class TestWeekSetCurrentEndpoint:
    def _url(self, plan, week):
        return reverse(
            "meso:api_week_set_current",
            kwargs={"plan_id": plan.pk, "week_id": week.pk},
        )

    def _two_week_plan(self):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso = plan.mesocycles.get()
        return link, plan, meso.weeks.get(index=1), meso.append_week()

    def test_sets_target_current_and_clears_siblings(self, client):
        link, plan, week1, week2 = self._two_week_plan()
        assert week1.is_current is True
        client.force_login(link.coach)
        resp = client.post(self._url(plan, week2))
        assert resp.status_code == 200
        week1.refresh_from_db()
        week2.refresh_from_db()
        assert week2.is_current is True
        assert week1.is_current is False
        body = resp.json()
        assert body["viewing"] == week2.pk
        # The strip flags exactly the new current week.
        current = [w for w in body["weeks"] if w["current"]]
        assert [w["id"] for w in current] == [week2.pk]

    def test_bumps_plan_modified(self, client):
        link, plan, week1, week2 = self._two_week_plan()
        before = plan.modified
        client.force_login(link.coach)
        client.post(self._url(plan, week2))
        plan.refresh_from_db()
        assert plan.modified > before

    def test_over_limit_suspended_plan_is_402(self, client):
        coach = UserFactory()
        _aged_link(coach, days_ago=30)
        suspended = _aged_link(coach, days_ago=1)
        plan = suspended.create_plan()
        meso = plan.mesocycles.get()
        week2 = meso.append_week()
        client.force_login(coach)
        resp = client.post(self._url(plan, week2))
        assert resp.status_code == 402
        week2.refresh_from_db()
        assert week2.is_current is False

    def test_foreign_coach_forbidden(self, client):
        link, plan, week1, week2 = self._two_week_plan()
        client.force_login(UserFactory())
        assert client.post(self._url(plan, week2)).status_code in (403, 404)

    def test_404_for_a_week_in_another_plan(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        other_week = WeekFactory()
        client.force_login(link.coach)
        assert client.post(self._url(plan, other_week)).status_code == 404

    def test_rejects_get(self, client):
        link, plan, week1, week2 = self._two_week_plan()
        client.force_login(link.coach)
        assert client.get(self._url(plan, week2)).status_code == 405


# ---------------------------------------------------------------------------
# Groups — the shared program is multi-week too
# ---------------------------------------------------------------------------


class TestGroupWeekManagement:
    def test_week_add_on_a_group_plan_copies_the_shared_structure(self, client):
        group = MesoGroupFactory()
        plan = group.create_shared_plan()
        before = Week.objects.filter(mesocycle__plan=plan).count()
        client.force_login(group.coach)
        resp = client.post(reverse("meso:api_week_add", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 201
        assert Week.objects.filter(mesocycle__plan=plan).count() == before + 1
        new_week = _latest_week(plan)
        # The shared structure is copied so the new week is immediately editable.
        assert new_week.sessions.count() >= 1


# ---------------------------------------------------------------------------
# Designer wiring — the switcher strip is rendered + the JS exposes the verbs
# (source/render-level, per the test_designer_onboarding.py precedent)
# ---------------------------------------------------------------------------


def _read_designer_template():
    path = Path(__file__).resolve().parents[2] / "templates" / "meso" / "designer.html"
    return path.read_text()


def _read_meso_js():
    path = finders.find("js/meso.js")
    assert path, "static js/meso.js must resolve"
    return Path(path).read_text()


class TestSwitcherWiring:
    def test_designer_renders_the_week_switcher(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "switchWeek(" in body
        assert "addWeek()" in body
        assert "setCurrentWeek(" in body

    def test_template_wires_all_three_verbs(self):
        tpl = _read_designer_template()
        assert 'x-show="live && weeks.length"' in tpl
        assert "weekIsViewed(w)" in tpl

    def test_meso_js_exposes_the_week_methods(self):
        js = _read_meso_js()
        for verb in ("switchWeek", "addWeek", "setCurrentWeek", "applyPlanData"):
            assert verb in js
