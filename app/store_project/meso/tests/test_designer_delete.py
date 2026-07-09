"""Phase 0a — soft-delete + delete endpoints (issue #401).

The designer needs a way to remove an exercise row, a training day, or a whole
week without destroying the athlete's training history: a coach who deletes a
week the athlete already logged against must not take the ``SessionLog``/
``LoggedSet`` rows with it (Phase 1's undo restores by flipping a flag, so a
cascading hard-delete would be unrecoverable and would resurrect unrelated
rows). The chosen shape is a single ``deleted_at`` flag on ``Week``/
``Session``/``ExercisePrescription``: a row is *live* iff ``deleted_at IS
NULL``, and a delete endpoint stamps **only the target row** — children of a
deleted row are hidden implicitly because every read (serializers + view
lookups) filters live rows at each level of the walk, not because the delete
cascaded a write onto them.

Covers, per ``docs/archive/meso/designer-framework-plan.md`` Phase 0:

- the three new endpoints (``api_prescription_delete``/``api_session_delete``/
  ``api_week_delete``): happy path (200, envelope shape, DB row survives with
  ``deleted_at`` set, drops out of ``serialize_plan``, bumps ``plan.modified``)
  and the shared negative matrix (login/method/ownership/404/double-delete);
- the week-delete extra rules (can't delete the current week or the last live
  week; deleting the viewed week reopens onto the current one);
- every existing lookup that must become soft-delete-aware once a row can be
  hidden without being gone (``prescription_patch``, ``session_add_exercise``,
  ``week_view``, ``week_set_current``, ``plan_deliver``, ``week_add`` after a
  delete, ``Mesocycle.append_week``, ``serialize_week_snapshot``);
- group plans: the group coach can delete on the shared program, and
  ``GroupMembership.sync_delivered_plan`` never materializes a soft-deleted
  source row onto a member's plan.
"""

import json

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import GroupMembershipFactory
from store_project.meso.factories import GroupPlanFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.models import Week
from store_project.meso.serializers import serialize_plan
from store_project.meso.serializers import serialize_week_snapshot
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


def seed_plan(coach=None, athlete=None):
    """A minimal owned plan with one current week → session → prescription cell."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    cell = presc(session, name="Box Squat", sets="4", reps="6", load="70", rpe="7")
    return plan, week, session, cell


def seed_group_plan():
    """A minimal owned group plan with one current week → session → prescription cell."""
    group = MesoGroupFactory()
    plan = GroupPlanFactory(group=group, title="Squad Block", status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    cell = presc(session, name="Box Squat")
    return group, plan, week, session, cell


def _two_week_plan():
    """A plan with ``week1`` (current) and ``week2`` (non-current)."""
    link = CoachAthleteFactory()
    plan = link.create_plan()
    meso = plan.mesocycles.get()
    week1 = meso.weeks.get(index=1)
    week2 = meso.append_week()
    return link, plan, week1, week2


def _envelope_keys(body):
    return {"program", "weeks", "viewing", "phases"}.issubset(body.keys())


# ---------------------------------------------------------------------------
# POST /meso/api/plan/<id>/prescription/<pk>/delete/
# ---------------------------------------------------------------------------


class TestPrescriptionDeleteEndpoint:
    def _url(self, plan, cell):
        return reverse(
            "meso:api_prescription_delete",
            kwargs={"plan_id": plan.pk, "pk": cell.pk},
        )

    def test_soft_deletes_and_returns_plan_envelope(self, client):
        plan, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)
        before = plan.modified
        resp = client.post(self._url(plan, cell))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert _envelope_keys(body)
        # Soft delete — the row's ExerciseSlot survives with the flag set
        # (block-wide identity), not hard-deleted.
        assert ExerciseSlot.objects.filter(pk=cell.exercise_slot_id).exists()
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.deleted_at is not None
        # It no longer surfaces in the serialized plan.
        data = serialize_plan(plan)
        ex_ids = [e["id"] for day in data["program"] for e in day["exercises"]]
        assert cell.pk not in ex_ids
        plan.refresh_from_db()
        assert plan.modified > before

    def test_pins_response_to_the_prescriptions_week(self, client):
        # The response is pinned to the deleted row's own week, not necessarily
        # the plan's current week.
        link, plan, week1, week2 = _two_week_plan()
        session2 = week2.sessions.first()
        cell2 = list(session2.cells())[0]
        client.force_login(link.coach)
        resp = client.post(self._url(plan, cell2))
        assert resp.status_code == 200
        assert resp.json()["viewing"] == week2.pk

    def test_delete_removes_the_row_from_every_week_block_wide(self, client):
        # P0 fixed-lineup semantics: a row's identity (ExerciseSlot) is shared
        # across every week of the block, so deleting it via one week's cell
        # removes the row from ALL weeks, not just the one being viewed.
        link, plan, week1, week2 = _two_week_plan()
        session1 = week1.sessions.first()
        cell1 = list(session1.cells())[0]
        session2 = week2.sessions.first()
        cell2 = list(session2.cells())[0]
        assert cell1.exercise_slot_id == cell2.exercise_slot_id
        client.force_login(link.coach)
        resp = client.post(self._url(plan, cell1))
        assert resp.status_code == 200
        # Both weeks' cells for this row are now hidden — not just week1's.
        data1 = serialize_plan(plan, week=week1)
        data2 = serialize_plan(plan, week=week2)
        ex_ids_1 = [e["id"] for d in data1["program"] for e in d["exercises"]]
        ex_ids_2 = [e["id"] for d in data2["program"] for e in d["exercises"]]
        assert cell1.pk not in ex_ids_1
        assert cell2.pk not in ex_ids_2

    def test_requires_login(self, client):
        plan, week, session, cell = seed_plan()
        resp = client.post(self._url(plan, cell))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        plan, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.get(self._url(plan, cell))
        assert resp.status_code == 405

    def test_non_owner_forbidden(self, client):
        plan, week, session, cell = seed_plan()
        client.force_login(UserFactory())
        resp = client.post(self._url(plan, cell))
        assert resp.status_code == 403
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.deleted_at is None

    def test_unknown_pk_404(self, client):
        plan, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            reverse(
                "meso:api_prescription_delete",
                kwargs={"plan_id": plan.pk, "pk": 999999},
            )
        )
        assert resp.status_code == 404

    def test_prescription_under_someone_elses_plan_404(self, client):
        plan, _, _, _ = seed_plan()
        _, _, _, other_cell = seed_plan()  # a different coach's plan
        client.force_login(plan.relationship.coach)
        resp = client.post(self._url(plan, other_cell))
        assert resp.status_code == 404

    def test_double_delete_404(self, client):
        plan, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)
        first = client.post(self._url(plan, cell))
        assert first.status_code == 200
        second = client.post(self._url(plan, cell))
        assert second.status_code == 404


# ---------------------------------------------------------------------------
# POST /meso/api/plan/<id>/session/<pk>/delete/
# ---------------------------------------------------------------------------


class TestSessionDeleteEndpoint:
    def _url(self, plan, session):
        return reverse(
            "meso:api_session_delete",
            kwargs={"plan_id": plan.pk, "pk": session.pk},
        )

    def test_soft_deletes_and_returns_plan_envelope(self, client):
        plan, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)
        before = plan.modified
        resp = client.post(self._url(plan, session))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert _envelope_keys(body)
        assert Session.objects.filter(pk=session.pk).exists()
        session.refresh_from_db()
        assert session.deleted_at is not None
        data = serialize_plan(plan)
        day_ids = [d["id"] for d in data["program"]]
        assert session.pk not in day_ids
        plan.refresh_from_db()
        assert plan.modified > before

    def test_pins_response_to_the_sessions_week(self, client):
        link, plan, week1, week2 = _two_week_plan()
        session2 = week2.sessions.first()
        client.force_login(link.coach)
        resp = client.post(self._url(plan, session2))
        assert resp.status_code == 200
        assert resp.json()["viewing"] == week2.pk

    def test_delete_removes_the_day_from_every_week_block_wide(self, client):
        # P0 fixed-lineup semantics: a day's identity (SessionSlot) is shared
        # across every week of the block, so deleting it via one week's
        # session removes the day from ALL weeks, not just the one viewed.
        link, plan, week1, week2 = _two_week_plan()
        session1 = week1.sessions.first()
        session2 = week2.sessions.first()
        assert session1.session_slot_id == session2.session_slot_id
        client.force_login(link.coach)
        resp = client.post(self._url(plan, session1))
        assert resp.status_code == 200
        session1.refresh_from_db()
        session2.refresh_from_db()
        assert session1.deleted_at is not None
        assert session2.deleted_at is not None

    def test_preserves_session_logs_and_logged_sets(self, client):
        plan, week, session, cell = seed_plan()
        log = SessionLogFactory(session=session, athlete=plan.athlete)
        logged_set = LoggedSetFactory(session_log=log, prescription=cell)
        client.force_login(plan.relationship.coach)
        resp = client.post(self._url(plan, session))
        assert resp.status_code == 200
        assert SessionLog.objects.filter(pk=log.pk).exists()
        assert LoggedSet.objects.filter(pk=logged_set.pk).exists()

    def test_requires_login(self, client):
        plan, week, session, cell = seed_plan()
        resp = client.post(self._url(plan, session))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        plan, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.get(self._url(plan, session))
        assert resp.status_code == 405

    def test_non_owner_forbidden(self, client):
        plan, week, session, cell = seed_plan()
        client.force_login(UserFactory())
        resp = client.post(self._url(plan, session))
        assert resp.status_code == 403
        session.refresh_from_db()
        assert session.deleted_at is None

    def test_unknown_pk_404(self, client):
        plan, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            reverse(
                "meso:api_session_delete", kwargs={"plan_id": plan.pk, "pk": 999999}
            )
        )
        assert resp.status_code == 404

    def test_session_under_someone_elses_plan_404(self, client):
        plan, _, _, _ = seed_plan()
        _, _, other_session, _ = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(self._url(plan, other_session))
        assert resp.status_code == 404

    def test_double_delete_404(self, client):
        plan, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)
        first = client.post(self._url(plan, session))
        assert first.status_code == 200
        second = client.post(self._url(plan, session))
        assert second.status_code == 404


# ---------------------------------------------------------------------------
# POST /meso/api/plan/<id>/week/<week_id>/delete/
# ---------------------------------------------------------------------------


class TestWeekDeleteEndpoint:
    def _url(self, plan, week):
        return reverse(
            "meso:api_week_delete",
            kwargs={"plan_id": plan.pk, "week_id": week.pk},
        )

    def test_soft_deletes_non_current_week_and_returns_current_weeks_payload(
        self, client
    ):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        before = plan.modified
        resp = client.post(self._url(plan, week2))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert _envelope_keys(body)
        # serialize_plan(plan) with no pinned week falls back to the current
        # week — the frontend's fallback per the spec.
        assert body["viewing"] == week1.pk
        assert [w["id"] for w in body["weeks"]] == [week1.pk]
        assert Week.objects.filter(pk=week2.pk).exists()
        week2.refresh_from_db()
        assert week2.deleted_at is not None
        plan.refresh_from_db()
        assert plan.modified > before

    def test_current_week_cannot_be_deleted(self, client):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        resp = client.post(self._url(plan, week1))
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        week1.refresh_from_db()
        assert week1.deleted_at is None

    def test_last_remaining_live_week_cannot_be_deleted(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso = plan.mesocycles.get()
        week = meso.weeks.get()
        # Force it non-current so this exercises the *last-live-week* rule, not
        # the separate "can't delete the current week" rule above.
        week.is_current = False
        week.save(update_fields=["is_current"])
        client.force_login(link.coach)
        resp = client.post(self._url(plan, week))
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        week.refresh_from_db()
        assert week.deleted_at is None

    def test_requires_login(self, client):
        link, plan, week1, week2 = _two_week_plan()
        resp = client.post(self._url(plan, week2))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        resp = client.get(self._url(plan, week2))
        assert resp.status_code == 405

    def test_non_owner_forbidden(self, client):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(UserFactory())
        resp = client.post(self._url(plan, week2))
        assert resp.status_code == 403
        week2.refresh_from_db()
        assert week2.deleted_at is None

    def test_unknown_week_id_404(self, client):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        resp = client.post(
            reverse(
                "meso:api_week_delete",
                kwargs={"plan_id": plan.pk, "week_id": 999999},
            )
        )
        assert resp.status_code == 404

    def test_week_under_someone_elses_plan_404(self, client):
        link, plan, week1, week2 = _two_week_plan()
        other_week = WeekFactory()  # belongs to a different plan entirely
        client.force_login(link.coach)
        resp = client.post(self._url(plan, other_week))
        assert resp.status_code == 404

    def test_double_delete_404(self, client):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        first = client.post(self._url(plan, week2))
        assert first.status_code == 200
        second = client.post(self._url(plan, week2))
        assert second.status_code == 404

    def test_week_add_succeeds_after_deleting_a_week(self, client):
        # week1 (index 1, current) stays live; week2 (index 2) is deleted. A
        # naive `source.index + 1` would collide with week2's still-unique
        # (mesocycle, index) row via `unique_week_index`, since a soft-deleted
        # week keeps its index — the new week must be indexed past it.
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        del_resp = client.post(self._url(plan, week2))
        assert del_resp.status_code == 200

        add_resp = client.post(
            reverse("meso:api_week_add", kwargs={"plan_id": plan.pk})
        )
        assert add_resp.status_code == 201
        meso = plan.mesocycles.get()
        newest = Week.objects.filter(mesocycle=meso).order_by("-pk").first()
        assert newest.index > week2.index
        assert newest.deleted_at is None


# ---------------------------------------------------------------------------
# Existing lookups that must become soft-delete-aware
# ---------------------------------------------------------------------------


class TestAncestorSoftDeleteAware:
    def test_prescription_patch_404s_once_its_session_is_deleted(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        del_url = reverse(
            "meso:api_session_delete", kwargs={"plan_id": plan.pk, "pk": session.pk}
        )
        assert client.post(del_url).status_code == 200

        patch_url = reverse(
            "meso:api_prescription_patch", kwargs={"plan_id": plan.pk, "pk": presc.pk}
        )
        resp = client.post(
            patch_url,
            data=json.dumps({"load": "999"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_session_add_exercise_404s_once_its_week_is_deleted(self, client):
        link, plan, week1, week2 = _two_week_plan()
        session2 = week2.sessions.first()
        client.force_login(link.coach)
        del_week_url = reverse(
            "meso:api_week_delete", kwargs={"plan_id": plan.pk, "week_id": week2.pk}
        )
        assert client.post(del_week_url).status_code == 200

        add_ex_url = reverse(
            "meso:api_session_add_exercise",
            kwargs={"plan_id": plan.pk, "pk": session2.pk},
        )
        resp = client.post(add_ex_url)
        assert resp.status_code == 404

    def test_week_view_404s_for_a_deleted_week(self, client):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        del_week_url = reverse(
            "meso:api_week_delete", kwargs={"plan_id": plan.pk, "week_id": week2.pk}
        )
        assert client.post(del_week_url).status_code == 200

        resp = client.get(
            reverse(
                "meso:api_week_view", kwargs={"plan_id": plan.pk, "week_id": week2.pk}
            )
        )
        assert resp.status_code == 404

    def test_week_set_current_404s_for_a_deleted_week(self, client):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        del_week_url = reverse(
            "meso:api_week_delete", kwargs={"plan_id": plan.pk, "week_id": week2.pk}
        )
        assert client.post(del_week_url).status_code == 200

        resp = client.post(
            reverse(
                "meso:api_week_set_current",
                kwargs={"plan_id": plan.pk, "week_id": week2.pk},
            )
        )
        assert resp.status_code == 404

    def test_plan_deliver_rejects_a_deleted_week(self, client):
        link, plan, week1, week2 = _two_week_plan()
        client.force_login(link.coach)
        del_week_url = reverse(
            "meso:api_week_delete", kwargs={"plan_id": plan.pk, "week_id": week2.pk}
        )
        assert client.post(del_week_url).status_code == 200

        resp = client.post(
            reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk}),
            data=json.dumps({"week_id": week2.pk}),
            content_type="application/json",
        )
        assert 400 <= resp.status_code < 500


class TestAppendWeekSoftDelete:
    def test_append_week_copies_only_live_sessions_and_prescriptions(self, client):
        link = CoachAthleteFactory()
        plan = link.create_plan()  # scaffold: 2 days, 1 row each
        meso = plan.mesocycles.get()
        week1 = meso.weeks.get(index=1)
        sessions = list(week1.sessions.order_by("session_slot__order"))
        assert len(sessions) == 2
        doomed_session, survivor = sessions[0], sessions[1]
        doomed_cell = presc(survivor, name="Doomed Row")

        client.force_login(link.coach)
        del_session_url = reverse(
            "meso:api_session_delete",
            kwargs={"plan_id": plan.pk, "pk": doomed_session.pk},
        )
        assert client.post(del_session_url).status_code == 200
        del_presc_url = reverse(
            "meso:api_prescription_delete",
            kwargs={"plan_id": plan.pk, "pk": doomed_cell.pk},
        )
        assert client.post(del_presc_url).status_code == 200

        new_week = meso.append_week()

        assert new_week.sessions.count() == 1
        copied_session = new_week.sessions.get()
        assert copied_session.day_number == survivor.day_number
        assert "Doomed Row" not in [c.name for c in copied_session.cells()]


class TestSerializeWeekSnapshotSoftDelete:
    def test_excludes_soft_deleted_sessions_and_prescriptions(self, client):
        plan, week, session, cell = seed_plan()
        other_session = day(week, day_number=2, name="Upper")
        keep_cell = presc(other_session, name="Bench")
        doomed_cell = presc(other_session, name="Doomed Row")

        client.force_login(plan.relationship.coach)
        del_session_url = reverse(
            "meso:api_session_delete", kwargs={"plan_id": plan.pk, "pk": session.pk}
        )
        assert client.post(del_session_url).status_code == 200
        del_presc_url = reverse(
            "meso:api_prescription_delete",
            kwargs={"plan_id": plan.pk, "pk": doomed_cell.pk},
        )
        assert client.post(del_presc_url).status_code == 200

        snapshot = serialize_week_snapshot(week)
        session_ids = [s["id"] for s in snapshot["sessions"]]
        assert session.pk not in session_ids
        assert other_session.pk in session_ids
        ex_ids = [e["id"] for s in snapshot["sessions"] for e in s["exercises"]]
        assert doomed_cell.pk not in ex_ids
        assert keep_cell.pk in ex_ids


# ---------------------------------------------------------------------------
# Group plans
# ---------------------------------------------------------------------------


class TestGroupPlanDelete:
    def test_group_coach_can_delete_prescription_and_session(self, client):
        group, plan, week, session, cell = seed_group_plan()
        client.force_login(group.coach)

        presc_resp = client.post(
            reverse(
                "meso:api_prescription_delete",
                kwargs={"plan_id": plan.pk, "pk": cell.pk},
            )
        )
        assert presc_resp.status_code == 200
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.deleted_at is not None

        session_resp = client.post(
            reverse(
                "meso:api_session_delete", kwargs={"plan_id": plan.pk, "pk": session.pk}
            )
        )
        assert session_resp.status_code == 200
        session.refresh_from_db()
        assert session.deleted_at is not None

    def test_sync_delivered_plan_skips_soft_deleted_source_rows(self, client):
        group, plan, week, session, cell = seed_group_plan()
        survivor = day(week, day_number=2, name="Upper")
        presc(survivor, name="Bench")
        membership = GroupMembershipFactory(group=group)

        client.force_login(group.coach)
        resp = client.post(
            reverse(
                "meso:api_session_delete", kwargs={"plan_id": plan.pk, "pk": session.pk}
            )
        )
        assert resp.status_code == 200

        member_plan, member_weeks = membership.sync_delivered_plan(week.mesocycle)
        member_week = member_weeks[0]
        day_numbers = [s.day_number for s in member_week.sessions.all()]
        assert session.day_number not in day_numbers
        assert survivor.day_number in day_numbers


class TestGroupRedeliverySoftDelete:
    """Re-delivering after a shared-program delete must not destroy member logs.

    ``sync_delivered_plan`` reconciles the member's materialized plan against
    the live source rows; a source row the coach soft-deleted must *hide* the
    member's copy (flag flip), never hard-delete it — ``SessionLog.session``
    cascades, so a hard delete erases the member's logged history on the next
    delivery. A source row that comes back (Phase 1 undo) revives the member's
    hidden copy in place, same pk.
    """

    def test_redelivery_hides_member_session_and_keeps_its_logs(self, client):
        group, plan, week, session, cell = seed_group_plan()
        membership = GroupMembershipFactory(group=group)
        _, member_weeks = membership.sync_delivered_plan(week.mesocycle)
        member_week = member_weeks[0]
        member_session = member_week.sessions.get(
            session_slot__day_number=session.day_number
        )
        member_cell = list(member_session.cells())[0]
        athlete = membership.relationship.athlete
        log = SessionLogFactory(session=member_session, athlete=athlete)
        logged_set = LoggedSetFactory(session_log=log, prescription=member_cell)

        client.force_login(group.coach)
        resp = client.post(
            reverse(
                "meso:api_session_delete", kwargs={"plan_id": plan.pk, "pk": session.pk}
            )
        )
        assert resp.status_code == 200
        membership.sync_delivered_plan(week.mesocycle)

        member_session.refresh_from_db()
        assert member_session.deleted_at is not None
        log.refresh_from_db()
        assert log.session_id == member_session.pk
        logged_set.refresh_from_db()
        assert logged_set.session_log_id == log.pk

        # Simulate Phase 1 undo: the source day returns (its identity is the
        # block-shared SessionSlot, cascaded onto its rows/sessions by
        # ``session_delete``) → the member's hidden copy is revived in place
        # (same pk, flag cleared), not recreated.
        source_slot = session.session_slot
        source_slot.deleted_at = None
        source_slot.save(update_fields=["deleted_at"])
        source_slot.exercise_slots.update(deleted_at=None)
        source_slot.sessions.update(deleted_at=None)
        membership.sync_delivered_plan(week.mesocycle)
        member_session.refresh_from_db()
        assert member_session.deleted_at is None
        assert (
            member_week.sessions.filter(
                session_slot__day_number=session.day_number
            ).count()
            == 1
        )

    def test_redelivery_hides_member_prescription_rows(self, client):
        group, plan, week, session, cell = seed_group_plan()
        extra = presc(session, name="Curl", order=7)
        membership = GroupMembershipFactory(group=group)
        _, member_weeks = membership.sync_delivered_plan(week.mesocycle)
        member_week = member_weeks[0]
        member_session = member_week.sessions.get(
            session_slot__day_number=session.day_number
        )
        member_extra = next(c for c in member_session.cells() if c.name == "Curl")

        client.force_login(group.coach)
        resp = client.post(
            reverse(
                "meso:api_prescription_delete",
                kwargs={"plan_id": plan.pk, "pk": extra.pk},
            )
        )
        assert resp.status_code == 200
        membership.sync_delivered_plan(week.mesocycle)

        member_extra.exercise_slot.refresh_from_db()
        assert member_extra.exercise_slot.deleted_at is not None


class TestDeliverScreenSoftDelete:
    """The deliver confirmation screen must not offer removed weeks or days.

    The deliver POST already 404s a soft-deleted target; the *screen* has to
    agree — its week selector lists live weeks only, a ``?week=`` pointing at a
    removed week falls back to the live target (same lenient contract as a
    foreign week), and the session count ignores removed days.
    """

    def test_week_selector_lists_live_weeks_only(self):
        link, plan, week1, week2 = _two_week_plan()
        week2.deleted_at = timezone.now()
        week2.save(update_fields=["deleted_at"])
        deliver = presenters.deliver_screen(plan)["deliver"]
        assert [w["id"] for w in deliver["weeks"]] == [week1.pk]

    def test_week_param_pointing_at_a_removed_week_falls_back_to_live(self, client):
        link, plan, week1, week2 = _two_week_plan()
        week2.deleted_at = timezone.now()
        week2.save(update_fields=["deleted_at"])
        client.force_login(link.coach)
        resp = client.get(
            reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
            + f"?week={week2.pk}"
        )
        assert resp.status_code == 200
        assert resp.context["deliver"]["week_id"] == week1.pk

    def test_session_count_ignores_removed_days(self):
        plan, week, session, cell = seed_plan()
        extra_session = day(week, day_number=2, name="Upper")
        extra_session.deleted_at = timezone.now()
        extra_session.save(update_fields=["deleted_at"])
        deliver = presenters.deliver_screen(plan)["deliver"]
        (wk,) = deliver["weeks"]
        assert wk["session_count"] == 1
