"""Multi-week designer — add weeks, view any week.

Until now a plan was effectively single-week: ``Plan.scaffold`` materialized one
``Week`` and the only growth verb was ``session_add`` (a day in *that* week). A
coach could not build a multi-week mesocycle or review an earlier week. This
slice closes that long-deferred gap:

- ``Mesocycle.append_week`` — materialize the next week, copying the latest week's
  session/prescription structure (a real progression starting point, not a blank).
- ``GET  /meso/api/plan/<id>/week/<week_id>/``          — view/edit any week (read).
- ``POST /meso/api/plan/<id>/week/``                    — add the next week (write).
- ``serialize_week`` gains ``id``/``index``; ``serialize_plan`` gains ``viewing``
  (the open week's id) so the client tracks which week's grid it is showing.

(The former ``POST .../week/<id>/current/`` "set the deliver target" endpoint —
the coach's manual "Make current" — was removed along with ``Week.is_current``
itself; see docs/meso/remove-current-week-plan.md. Programs are date-less and
every live week is simply deliverable.)
"""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Session
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
    def test_reuses_the_blocks_shared_day_and_row_identity(self):
        """P0: a new week never deep-copies identity.

        The SessionSlot/ExerciseSlot rows are shared block-wide, so
        ``append_week`` reuses the SAME day/row pks and only adds a fresh
        per-week cell whose numbers are carried forward from the latest
        live week.
        """
        link = CoachAthleteFactory()
        plan = link.create_plan()  # scaffold: 1 block, 1 week, 2 days, 1 row each
        meso = plan.mesocycles.get()
        source = meso.weeks.get()
        # Distinguish the source so the copy is unambiguous.
        source.phase = "Accum"
        source.volume = 88
        source.is_deload = False
        source.save()
        first_day = source.sessions.order_by("session_slot__order").first()
        first_cell = list(first_day.cells())[0]
        first_cell.exercise_slot.name = "Back Squat"
        first_cell.exercise_slot.tags = ["main"]
        first_cell.exercise_slot.save()
        first_cell.text = "3 x 5, 100%"
        first_cell.save()

        new_week = meso.append_week()

        assert new_week.index == 2
        assert new_week.delivered_at is None
        # Meta carried forward as a starting point.
        assert new_week.phase == "Accum"
        assert new_week.volume == 88
        # Same day IDENTITY (the SessionSlot rows are block-shared, not copied).
        src_days = list(source.sessions.order_by("session_slot__order"))
        new_days = list(new_week.sessions.order_by("session_slot__order"))
        assert [d.session_slot_id for d in new_days] == [
            d.session_slot_id for d in src_days
        ]
        assert [d.name for d in new_days] == [d.name for d in src_days]
        assert [d.day_number for d in new_days] == [d.day_number for d in src_days]
        # The new week's cell shares the SAME row identity (ExerciseSlot) but is
        # a fresh per-week cell whose numbers were carried forward.
        copied = list(new_days[0].cells())[0]
        assert copied.pk != first_cell.pk
        assert copied.exercise_slot_id == first_cell.exercise_slot_id
        assert copied.name == "Back Squat"
        assert copied.text == "3 x 5, 100%"
        assert copied.tags == ["main"]

    def test_new_week_is_not_delivered(self):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso = plan.mesocycles.get()
        meso.append_week()
        # The new week is live immediately (no pointer to move) but starts
        # undelivered — delivery is a separate, explicit nudge.
        assert meso.weeks.count() == 2
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
        assert new_week.sessions.get().cells().count() == 1


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

    # -----------------------------------------------------------------------
    # ``mesocycle_id`` — "+ Add week" targets the block the coach is VIEWING,
    # not whichever block ``current_week`` used to re-derive (§4b FIX 2).
    # -----------------------------------------------------------------------

    def test_posted_mesocycle_id_targets_that_block(self, client):
        # The grid opens on whatever block the client already has loaded, and
        # the client always knows its id — the designer sends it explicitly so
        # "+ Add week" can never disagree with what's on screen.
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso1 = plan.mesocycles.get()
        meso2 = MesocycleFactory(plan=plan, order=1)
        WeekFactory(mesocycle=meso2, index=1)
        client.force_login(link.coach)

        resp = client.post(
            self._url(plan),
            data=json.dumps({"mesocycle_id": meso2.pk}),
            content_type="application/json",
        )

        assert resp.status_code == 201
        assert meso2.weeks.count() == 2  # grew
        assert meso1.weeks.count() == 1  # untouched

    def test_foreign_mesocycle_id_is_404(self, client):
        # ``plan=plan`` in the lookup IS the security check — a block that
        # belongs to some other plan must 404 exactly like an unknown id
        # would, never silently fall back to one of THIS plan's blocks.
        link = CoachAthleteFactory()
        plan = link.create_plan()
        foreign_meso = MesocycleFactory()  # a different plan entirely
        client.force_login(link.coach)

        resp = client.post(
            self._url(plan),
            data=json.dumps({"mesocycle_id": foreign_meso.pk}),
            content_type="application/json",
        )

        assert resp.status_code == 404
        assert Week.objects.filter(mesocycle=foreign_meso).count() == 0

    def test_non_integer_mesocycle_id_is_400(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)

        resp = client.post(
            self._url(plan),
            data=json.dumps({"mesocycle_id": "not-a-number"}),
            content_type="application/json",
        )

        assert resp.status_code == 400

    def test_bodyless_add_targets_the_plans_first_block_even_when_it_has_no_live_weeks(
        self, client
    ):
        """REGRESSION GUARD: must not revert to ``current_week(plan).mesocycle``.

        The old fallback picked the block of the plan's earliest LIVE week —
        which is block 2 whenever block 1 hasn't been materialized yet. That
        disagreed with the grid, which opens on ``_default_grid_mesocycle``
        (the plan's first block, by ``order``, full stop): the coach would see
        an empty block 1 on screen while "+ Add week" silently grew block 2,
        leaving the new week unreachable from the grid the coach was looking
        at. A bodyless post must land on block 1 regardless of which block
        happens to have live weeks.
        """
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso1 = plan.mesocycles.get()
        meso1.weeks.all().delete()  # block 1: no live weeks (the empty grid)
        meso2 = MesocycleFactory(plan=plan, order=1)
        WeekFactory(mesocycle=meso2, index=1)  # block 2: has a live week
        client.force_login(link.coach)

        resp = client.post(self._url(plan))  # no body

        assert resp.status_code == 201
        assert meso1.weeks.count() == 1  # landed on the FIRST block...
        assert meso2.weeks.count() == 1  # ...block 2 is untouched


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
        week2 = meso.append_week()
        # Day/row identity (``SessionSlot``/``ExerciseSlot``) is block-shared
        # now, so the only P0-correct way to tell week1's and week2's *served
        # program* apart is by their per-week cell text, not a name.
        cell1 = list(week1.sessions.order_by("session_slot__order").first().cells())[0]
        cell1.text = "3 x 10, 101"
        cell1.save()
        cell2 = list(week2.sessions.order_by("session_slot__order").first().cells())[0]
        cell2.text = "3 x 10, 202"
        cell2.save()
        return link, plan, week1, week2

    def test_returns_the_target_weeks_program(self, client):
        link, plan, week1, week2 = self._two_week_plan()
        client.force_login(link.coach)
        resp = client.get(self._url(plan, week2))
        assert resp.status_code == 200
        body = resp.json()
        assert body["viewing"] == week2.pk
        texts = [ex["text"] for d in body["program"] for ex in d["exercises"]]
        assert "3 x 10, 202" in texts
        assert "3 x 10, 101" not in texts
        # Both days are still block-shared, so the same two day slots show up
        # for either week — this is the P0 semantics, not a stale assertion.
        assert len(body["program"]) == 2
        assert len(body["weeks"]) == 2

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
# session_add is week-scoped — "+ Add day" lands on the *viewed* week, not the
# default one (regression: the switcher can open a non-default week)
# ---------------------------------------------------------------------------


class TestSessionAddWeekScoping:
    def _url(self, plan):
        return reverse("meso:api_session_add", kwargs={"plan_id": plan.pk})

    def _two_week_plan(self):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso = plan.mesocycles.get()
        return link, plan, meso.weeks.get(index=1), meso.append_week()

    def test_adds_the_day_to_the_posted_week(self, client):
        link, plan, week1, week2 = self._two_week_plan()  # week1 is earliest
        client.force_login(link.coach)
        resp = client.post(
            self._url(plan),
            data=json.dumps({"week_id": week2.pk}),
            content_type="application/json",
        )
        assert resp.status_code == 201
        new_session = Session.objects.get(pk=resp.json()["session"]["id"])
        # The day lands on the viewed week, not the default one.
        assert new_session.week_id == week2.pk

    def test_defaults_to_the_earliest_week_without_a_week_id(self, client):
        link, plan, week1, week2 = self._two_week_plan()
        client.force_login(link.coach)
        resp = client.post(self._url(plan))  # no body → earliest live week
        assert resp.status_code == 201
        new_session = Session.objects.get(pk=resp.json()["session"]["id"])
        assert new_session.week_id == week1.pk

    def test_404_for_a_week_in_another_plan(self, client):
        link, plan, week1, week2 = self._two_week_plan()
        foreign = WeekFactory()
        client.force_login(link.coach)
        resp = client.post(
            self._url(plan),
            data=json.dumps({"week_id": foreign.pk}),
            content_type="application/json",
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Designer wiring — the week switcher is rendered + the JS exposes the verbs
# (source/render-level, per the test_designer_onboarding.py precedent).
#
# Phase 2 PR B originally moved this wiring from designer.html/meso.js to the
# React island's usePlanData.ts/WeekStrip.tsx. Issue #455 phase A5 retired
# those one-week files: the week switcher now lives inside the multi-week
# table itself (components/MesoTable.tsx's WeekColumnHeader) and the verbs
# are exposed by hooks/useGrid.ts, the island's sole data owner.
# ---------------------------------------------------------------------------


def _read_designer_template():
    path = Path(__file__).resolve().parents[2] / "templates" / "meso" / "designer.html"
    return path.read_text()


def _read_island_source(*parts):
    src = Path(__file__).resolve().parents[4] / "frontend" / "designer" / "src"
    return src.joinpath(*parts).read_text()


class TestSwitcherWiring:
    def test_designer_renders_the_mount_point_for_a_plan_with_weeks(self, client):
        # The week switcher itself is client-rendered (MesoTable.tsx, one
        # column header per week) — the server-side seam is that the plan's
        # weeks are still hydrated into the page for the island to read.
        link = CoachAthleteFactory()
        plan = link.create_plan()
        client.force_login(link.coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'id="meso-grid-data"' in body
        assert 'id="meso-designer-root"' in body

    def test_meso_table_wires_add_and_remove_week(self):
        # The mesocycle-level WeekManagerStrip renders each week's "remove"
        # control and the toolbar's "+ Add week" once (mirrors WeekStrip.tsx's
        # pre-A5 verbs: switch is now simply rendering every week as a
        # column, so there's no onSwitchWeek left). "Make current"/
        # onSetCurrentWeek was removed along with Week.is_current — see
        # docs/meso/remove-current-week-plan.md.
        table = _read_island_source("components", "MesoTable.tsx")
        assert "onAddWeek" in table
        assert "onRemoveWeek" in table
        assert "onSetCurrentWeek" not in table

    def test_use_grid_exposes_the_week_methods(self):
        js = _read_island_source("hooks", "useGrid.ts")
        for verb in ("addWeek", "removeWeek", "refetchGrid"):
            assert verb in js
        assert "setCurrentWeek" not in js
