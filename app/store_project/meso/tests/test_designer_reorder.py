"""Phase 4 — dnd-kit reorder endpoints (RED, designer framework Phase 4, #403).

The designer's drag-and-drop needs three write endpoints, built on Phase 0's
soft delete + Phase 1's undo op-log (``docs/archive/meso/designer-framework-plan.md``
Phase 4):

- ``api_session_reorder`` — reorder the exercise rows within one session
  (``Session.prescriptions``), by posting the session's full live prescription
  id set in the new order;
- ``api_week_reorder_sessions`` — reorder the training days within one week
  (``Week.sessions``), by posting the week's full live session id set in the
  new order;
- ``api_prescription_move`` — move one exercise row to a different session
  **within the same week** (cross-day drag), re-pointing ``session`` and
  densely renumbering both the source and target session's live rows.

All three follow the established designer-write shape: ``@login_required
@require_POST``, ``_editable_plan_or_response`` (302/403/402), live-row
scoping on the URL target and its ancestors (404), ``record_plan_action``
before the write (undo-able), ``_touch_plan``, and a
``{"ok": true, **serialize_plan(plan, week=...)}`` envelope. ``Session.order``/
``ExercisePrescription.order`` are 0-based, matching the scaffold's
``order=0`` convention (``Plan.scaffold``, ``Mesocycle.append_week``).

Covers, per the Phase 4 spec's pytest paragraph:

- per-endpoint happy path (DB order values + serialized ``program`` order +
  envelope keys + ``history.can_undo``), an idempotent repost, the id-set
  validation matrix (missing/extra/duplicate/foreign-plan/soft-deleted id,
  malformed JSON, non-int entries), the shared auth matrix (302/405/403/404),
  soft-deleted target/ancestor 404s, exactly one ``PlanAction`` per call with
  a loosely-pinned label, and an undo round trip;
- ``api_prescription_move``'s own rules: cross-week moves are a 400, the
  index clamps into range, a target-equals-source move behaves like a plain
  reorder, and a moved row's ``LoggedSet`` history keeps pointing at the same
  prescription pk untouched.

Where the spec is silent, decisions taken here (noted inline at the relevant
test): malformed-JSON/non-list/non-int-entry failures are asserted as a bare
400 (no JSON-body shape pinned) — mirroring ``prescription_patch``'s
``HttpResponseBadRequest`` convention for structurally-invalid bodies, as
opposed to the *semantic* id-set mismatches (missing/extra/duplicate/foreign/
deleted), which the spec explicitly promises as ``{"ok": false, "error": ...}``
and so are asserted as such here. ``api_prescription_move``'s ``session_id``
referencing a foreign/soft-deleted/nonexistent session is likewise asserted as
a 400 (a body-referenced id failing validation), mirroring
``prescription_override``'s ``_group_member_or_none`` 400-on-bad-reference
convention rather than the URL-segment 404 convention.
"""

import json

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import PlanAction
from store_project.meso.serializers import serialize_plan
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


def seed_session(n=3, coach=None, athlete=None):
    """A plan w/ one current week -> one session holding ``n`` live rows, order 0..n-1."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower", order=0)
    cells = [presc(session, name=f"Exercise {i}", order=i) for i in range(n)]
    return plan, week, session, cells


def seed_week_with_sessions(n=3, coach=None, athlete=None):
    """A plan w/ one current week holding ``n`` live sessions, order 0..n-1."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    sessions = [
        day(week, day_number=i + 1, name=f"Day {i + 1}", order=i) for i in range(n)
    ]
    return plan, week, sessions


def seed_two_sessions(coach=None, athlete=None):
    """One current week: ``session_a`` (2 rows: p0, p1), ``session_b`` (1 row: q0)."""
    plan, week, sessions = seed_week_with_sessions(n=2, coach=coach, athlete=athlete)
    session_a, session_b = sessions
    p0 = presc(session_a, name="Box Squat", order=0)
    p1 = presc(session_a, name="RDL", order=1)
    q0 = presc(session_b, name="Bench", order=0)
    return plan, week, session_a, session_b, p0, p1, q0


def _two_week_plan():
    """A plan with ``week1`` (current) and ``week2`` (non-current), scaffold each."""
    link = CoachAthleteFactory()
    plan = link.create_plan()
    meso = plan.mesocycles.get()
    week1 = meso.weeks.get(index=1)
    week2 = meso.append_week()
    return link, plan, week1, week2


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def undo_url(plan):
    return reverse("meso:api_plan_undo", kwargs={"plan_id": plan.pk})


def undo_actions(plan):
    return PlanAction.objects.filter(plan=plan, stack=PlanAction.Stack.UNDO)


def _envelope_keys(body):
    return {"program", "weeks", "viewing", "phases"}.issubset(body.keys())


def _session_order_url(plan, session):
    return reverse(
        "meso:api_session_reorder", kwargs={"plan_id": plan.pk, "pk": session.pk}
    )


def _week_order_url(plan, week):
    return reverse(
        "meso:api_week_reorder_sessions",
        kwargs={"plan_id": plan.pk, "week_id": week.pk},
    )


def _move_url(plan, cell):
    return reverse(
        "meso:api_prescription_move", kwargs={"plan_id": plan.pk, "pk": cell.pk}
    )


# ---------------------------------------------------------------------------
# POST /meso/api/plan/<id>/session/<pk>/reorder/
# ---------------------------------------------------------------------------


class TestSessionReorderEndpoint:
    def test_happy_path_writes_db_order_and_serializes_new_order(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _session_order_url(plan, session), {"order": [p2.pk, p0.pk, p1.pk]}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert _envelope_keys(body)
        assert body["history"]["can_undo"] is True

        p0.refresh_from_db()
        p1.refresh_from_db()
        p2.refresh_from_db()
        assert (
            p2.exercise_slot.order,
            p0.exercise_slot.order,
            p1.exercise_slot.order,
        ) == (
            0,
            1,
            2,
        )

        data = serialize_plan(plan, week=week)
        day_data = next(d for d in data["program"] if d["id"] == session.pk)
        assert [e["id"] for e in day_data["exercises"]] == [p2.pk, p0.pk, p1.pk]

    def test_idempotent_repost_is_a_200_noop_that_still_records_one_action(
        self, client
    ):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        client.force_login(plan.relationship.coach)
        url = _session_order_url(plan, session)
        current_order = [p0.pk, p1.pk, p2.pk]
        resp = post_json(client, url, {"order": current_order})
        assert resp.status_code == 200
        assert undo_actions(plan).count() == 1

        resp = post_json(client, url, {"order": current_order})
        assert resp.status_code == 200
        assert undo_actions(plan).count() == 2
        p0.refresh_from_db()
        p1.refresh_from_db()
        p2.refresh_from_db()
        assert (
            p0.exercise_slot.order,
            p1.exercise_slot.order,
            p2.exercise_slot.order,
        ) == (
            0,
            1,
            2,
        )

    def test_missing_id_400(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _session_order_url(plan, session), {"order": [p0.pk, p1.pk]}
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_extra_id_from_a_sibling_session_400(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        other_session = day(week, day_number=2, name="Upper", order=1)
        other_cell = presc(other_session, name="Bench")
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            _session_order_url(plan, session),
            {"order": [p0.pk, p1.pk, p2.pk, other_cell.pk]},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_duplicate_id_400(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _session_order_url(plan, session), {"order": [p0.pk, p0.pk, p1.pk]}
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_foreign_plans_id_400(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        _, _, _, (other_cell, *_rest) = seed_session(n=1)  # a different coach's plan
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            _session_order_url(plan, session),
            {"order": [p0.pk, p1.pk, other_cell.pk]},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_soft_deleted_id_in_list_400(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        client.force_login(plan.relationship.coach)
        del_resp = client.post(
            reverse(
                "meso:api_prescription_delete", kwargs={"plan_id": plan.pk, "pk": p2.pk}
            )
        )
        assert del_resp.status_code == 200
        # The live set is now just {p0, p1} — posting all three (including the
        # now-deleted p2) is an "extra" id relative to the live set.
        resp = post_json(
            client, _session_order_url(plan, session), {"order": [p0.pk, p1.pk, p2.pk]}
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_malformed_json_400(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        client.force_login(plan.relationship.coach)
        resp = client.post(
            _session_order_url(plan, session),
            data="not json",
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_non_list_order_400(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(client, _session_order_url(plan, session), {"order": "abc"})
        assert resp.status_code == 400

    def test_non_int_entries_400(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            _session_order_url(plan, session),
            {"order": [p0.pk, "not-an-id", p2.pk]},
        )
        assert resp.status_code == 400

    def test_requires_login(self, client):
        plan, week, session, prescs = seed_session(n=1)
        resp = post_json(client, _session_order_url(plan, session), {"order": []})
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        plan, week, session, prescs = seed_session(n=1)
        client.force_login(plan.relationship.coach)
        resp = client.get(_session_order_url(plan, session))
        assert resp.status_code == 405

    def test_non_owner_forbidden(self, client):
        plan, week, session, (p0,) = seed_session(n=1)
        client.force_login(UserFactory())
        resp = post_json(client, _session_order_url(plan, session), {"order": [p0.pk]})
        assert resp.status_code == 403

    def test_unknown_session_pk_404(self, client):
        plan, week, session, prescs = seed_session(n=1)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            reverse(
                "meso:api_session_reorder",
                kwargs={"plan_id": plan.pk, "pk": 999999},
            ),
            {"order": []},
        )
        assert resp.status_code == 404

    def test_soft_deleted_session_404(self, client):
        plan, week, session, (p0,) = seed_session(n=1)
        client.force_login(plan.relationship.coach)
        del_resp = client.post(
            reverse(
                "meso:api_session_delete", kwargs={"plan_id": plan.pk, "pk": session.pk}
            )
        )
        assert del_resp.status_code == 200
        resp = post_json(client, _session_order_url(plan, session), {"order": [p0.pk]})
        assert resp.status_code == 404

    def test_soft_deleted_ancestor_week_404(self, client):
        link, plan, week1, week2 = _two_week_plan()
        session2 = week2.sessions.first()
        cell2 = list(session2.cells())[0]
        client.force_login(link.coach)
        del_resp = client.post(
            reverse(
                "meso:api_week_delete", kwargs={"plan_id": plan.pk, "week_id": week2.pk}
            )
        )
        assert del_resp.status_code == 200
        resp = post_json(
            client, _session_order_url(plan, session2), {"order": [cell2.pk]}
        )
        assert resp.status_code == 404

    def test_records_exactly_one_plan_action_with_loosely_pinned_label(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _session_order_url(plan, session), {"order": [p2.pk, p0.pk, p1.pk]}
        )
        assert resp.status_code == 200
        actions = list(undo_actions(plan))
        assert len(actions) == 1
        assert actions[0].label == "Reordered exercises"

    def test_undo_round_trip_restores_original_order_values(self, client):
        plan, week, session, (p0, p1, p2) = seed_session(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _session_order_url(plan, session), {"order": [p2.pk, p0.pk, p1.pk]}
        )
        assert resp.status_code == 200

        resp = client.post(undo_url(plan))
        assert resp.status_code == 200
        p0.refresh_from_db()
        p1.refresh_from_db()
        p2.refresh_from_db()
        assert (
            p0.exercise_slot.order,
            p1.exercise_slot.order,
            p2.exercise_slot.order,
        ) == (
            0,
            1,
            2,
        )

    def test_reorder_is_visible_from_every_week_block_wide(self, client):
        # P0 fixed-lineup semantics: row order lives on the block-shared
        # ExerciseSlot, so reordering rows in one week's session reorders the
        # SAME day's rows in every other live week too — even one never
        # directly touched by this request.
        link = CoachAthleteFactory()
        plan = link.create_plan()
        meso = plan.mesocycles.get()
        week1 = meso.weeks.get(index=1)
        session1 = week1.sessions.order_by("session_slot__order").first()
        presc(session1, name="Second Row", order=1)
        week2 = meso.append_week()
        session2 = week2.sessions.get(session_slot=session1.session_slot)

        cells1 = list(session1.cells())
        assert len(cells1) == 2
        new_order = [cells1[1].pk, cells1[0].pk]
        client.force_login(link.coach)
        resp = post_json(
            client, _session_order_url(plan, session1), {"order": new_order}
        )
        assert resp.status_code == 200

        slot_order_week1 = [c.exercise_slot_id for c in session1.cells()]
        slot_order_week2 = [c.exercise_slot_id for c in session2.cells()]
        assert slot_order_week1 == slot_order_week2


# ---------------------------------------------------------------------------
# POST /meso/api/plan/<id>/week/<week_id>/reorder/
# ---------------------------------------------------------------------------


class TestWeekReorderSessionsEndpoint:
    def test_happy_path_writes_db_order_and_serializes_new_order(self, client):
        plan, week, (s0, s1, s2) = seed_week_with_sessions(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _week_order_url(plan, week), {"order": [s2.pk, s0.pk, s1.pk]}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert _envelope_keys(body)
        assert body["history"]["can_undo"] is True

        s0.refresh_from_db()
        s1.refresh_from_db()
        s2.refresh_from_db()
        assert (s2.order, s0.order, s1.order) == (0, 1, 2)
        # day_number/name stay untouched — order is presentation order only.
        assert s0.day_number == 1 and s0.name == "Day 1"

        data = serialize_plan(plan, week=week)
        assert [d["id"] for d in data["program"]] == [s2.pk, s0.pk, s1.pk]

    def test_idempotent_repost_is_a_200_noop_that_still_records_one_action(
        self, client
    ):
        plan, week, (s0, s1, s2) = seed_week_with_sessions(n=3)
        client.force_login(plan.relationship.coach)
        url = _week_order_url(plan, week)
        current_order = [s0.pk, s1.pk, s2.pk]
        resp = post_json(client, url, {"order": current_order})
        assert resp.status_code == 200
        assert undo_actions(plan).count() == 1

        resp = post_json(client, url, {"order": current_order})
        assert resp.status_code == 200
        assert undo_actions(plan).count() == 2
        s0.refresh_from_db()
        s1.refresh_from_db()
        s2.refresh_from_db()
        assert (s0.order, s1.order, s2.order) == (0, 1, 2)

    def test_missing_id_400(self, client):
        plan, week, (s0, s1, s2) = seed_week_with_sessions(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(client, _week_order_url(plan, week), {"order": [s0.pk, s1.pk]})
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_extra_id_from_a_sibling_week_400(self, client):
        link, plan, week1, week2 = _two_week_plan()
        session2 = week2.sessions.first()
        sessions1 = list(week1.sessions.all())
        client.force_login(link.coach)
        resp = post_json(
            client,
            _week_order_url(plan, week1),
            {"order": [s.pk for s in sessions1] + [session2.pk]},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_duplicate_id_400(self, client):
        plan, week, (s0, s1, s2) = seed_week_with_sessions(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _week_order_url(plan, week), {"order": [s0.pk, s0.pk, s1.pk]}
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_foreign_plans_id_400(self, client):
        plan, week, (s0, s1, s2) = seed_week_with_sessions(n=3)
        _, _, (other_session,) = seed_week_with_sessions(n=1)  # a different coach
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            _week_order_url(plan, week),
            {"order": [s0.pk, s1.pk, other_session.pk]},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_soft_deleted_id_in_list_400(self, client):
        plan, week, (s0, s1, s2) = seed_week_with_sessions(n=3)
        client.force_login(plan.relationship.coach)
        del_resp = client.post(
            reverse("meso:api_session_delete", kwargs={"plan_id": plan.pk, "pk": s2.pk})
        )
        assert del_resp.status_code == 200
        resp = post_json(
            client, _week_order_url(plan, week), {"order": [s0.pk, s1.pk, s2.pk]}
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_malformed_json_400(self, client):
        plan, week, (s0,) = seed_week_with_sessions(n=1)
        client.force_login(plan.relationship.coach)
        resp = client.post(
            _week_order_url(plan, week),
            data="not json",
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_non_list_order_400(self, client):
        plan, week, (s0,) = seed_week_with_sessions(n=1)
        client.force_login(plan.relationship.coach)
        resp = post_json(client, _week_order_url(plan, week), {"order": "abc"})
        assert resp.status_code == 400

    def test_non_int_entries_400(self, client):
        plan, week, (s0, s1, s2) = seed_week_with_sessions(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            _week_order_url(plan, week),
            {"order": [s0.pk, "not-an-id", s2.pk]},
        )
        assert resp.status_code == 400

    def test_requires_login(self, client):
        plan, week, (s0,) = seed_week_with_sessions(n=1)
        resp = post_json(client, _week_order_url(plan, week), {"order": []})
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        plan, week, (s0,) = seed_week_with_sessions(n=1)
        client.force_login(plan.relationship.coach)
        resp = client.get(_week_order_url(plan, week))
        assert resp.status_code == 405

    def test_non_owner_forbidden(self, client):
        plan, week, (s0,) = seed_week_with_sessions(n=1)
        client.force_login(UserFactory())
        resp = post_json(client, _week_order_url(plan, week), {"order": [s0.pk]})
        assert resp.status_code == 403

    def test_unknown_week_id_404(self, client):
        plan, week, (s0,) = seed_week_with_sessions(n=1)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            reverse(
                "meso:api_week_reorder_sessions",
                kwargs={"plan_id": plan.pk, "week_id": 999999},
            ),
            {"order": []},
        )
        assert resp.status_code == 404

    def test_soft_deleted_week_404(self, client):
        link, plan, week1, week2 = _two_week_plan()
        session2 = week2.sessions.first()
        client.force_login(link.coach)
        del_resp = client.post(
            reverse(
                "meso:api_week_delete", kwargs={"plan_id": plan.pk, "week_id": week2.pk}
            )
        )
        assert del_resp.status_code == 200
        resp = post_json(client, _week_order_url(plan, week2), {"order": [session2.pk]})
        assert resp.status_code == 404

    def test_records_exactly_one_plan_action_with_loosely_pinned_label(self, client):
        plan, week, (s0, s1, s2) = seed_week_with_sessions(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _week_order_url(plan, week), {"order": [s2.pk, s0.pk, s1.pk]}
        )
        assert resp.status_code == 200
        actions = list(undo_actions(plan))
        assert len(actions) == 1
        assert actions[0].label == "Reordered days"

    def test_undo_round_trip_restores_original_order_values(self, client):
        plan, week, (s0, s1, s2) = seed_week_with_sessions(n=3)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _week_order_url(plan, week), {"order": [s2.pk, s0.pk, s1.pk]}
        )
        assert resp.status_code == 200

        resp = client.post(undo_url(plan))
        assert resp.status_code == 200
        s0.refresh_from_db()
        s1.refresh_from_db()
        s2.refresh_from_db()
        assert (s0.order, s1.order, s2.order) == (0, 1, 2)

    def test_reorder_is_visible_from_every_week_block_wide(self, client):
        # P0 fixed-lineup semantics: a day's order lives on the block-shared
        # SessionSlot, so reordering days in one week reorders the SAME days
        # in every other live week too — even one never directly touched by
        # this request.
        link = CoachAthleteFactory()
        plan = link.create_plan()  # scaffold: 2 days
        meso = plan.mesocycles.get()
        week1 = meso.weeks.get(index=1)
        week2 = meso.append_week()
        sessions1 = list(week1.sessions.order_by("session_slot__order"))
        assert len(sessions1) == 2
        new_order = [sessions1[1].pk, sessions1[0].pk]
        client.force_login(link.coach)
        resp = post_json(client, _week_order_url(plan, week1), {"order": new_order})
        assert resp.status_code == 200

        slot_order_week1 = [
            s.session_slot_id for s in week1.sessions.order_by("session_slot__order")
        ]
        slot_order_week2 = [
            s.session_slot_id for s in week2.sessions.order_by("session_slot__order")
        ]
        assert slot_order_week1 == slot_order_week2


# ---------------------------------------------------------------------------
# POST /meso/api/plan/<id>/prescription/<pk>/move/
# ---------------------------------------------------------------------------


class TestPrescriptionMoveEndpoint:
    def test_happy_path_moves_row_and_densely_renumbers_both_sessions(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            _move_url(plan, p0),
            {"session_id": session_b.pk, "index": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert _envelope_keys(body)
        assert body["history"]["can_undo"] is True

        p0.refresh_from_db()
        p1.refresh_from_db()
        q0.refresh_from_db()
        assert p0.exercise_slot.session_slot_id == session_b.session_slot_id
        assert p0.exercise_slot.order == 0
        assert q0.exercise_slot.order == 1
        # session_a's remaining row renumbers densely (was order=1, now 0).
        assert p1.exercise_slot.session_slot_id == session_a.session_slot_id
        assert p1.exercise_slot.order == 0

        data = serialize_plan(plan, week=week)
        day_a = next(d for d in data["program"] if d["id"] == session_a.pk)
        day_b = next(d for d in data["program"] if d["id"] == session_b.pk)
        assert [e["id"] for e in day_a["exercises"]] == [p1.pk]
        assert [e["id"] for e in day_b["exercises"]] == [p0.pk, q0.pk]

    def test_idempotent_repost_of_the_same_move_is_a_200_noop(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        url = _move_url(plan, p0)
        resp = post_json(client, url, {"session_id": session_b.pk, "index": 0})
        assert resp.status_code == 200
        assert undo_actions(plan).count() == 1

        # p0 is now IN session_b at index 0 — reposting the identical move is a
        # target-equals-source no-op, still recording one action.
        resp = post_json(client, url, {"session_id": session_b.pk, "index": 0})
        assert resp.status_code == 200
        assert undo_actions(plan).count() == 2
        p0.refresh_from_db()
        assert p0.exercise_slot.session_slot_id == session_b.session_slot_id
        assert p0.exercise_slot.order == 0

    def test_target_equals_source_behaves_like_a_reorder(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": session_a.pk, "index": 1}
        )
        assert resp.status_code == 200
        p0.refresh_from_db()
        p1.refresh_from_db()
        q0.refresh_from_db()
        # p0 moved past p1 within the SAME session — a plain within-day reorder.
        assert p0.exercise_slot.session_slot_id == session_a.session_slot_id
        assert p1.exercise_slot.session_slot_id == session_a.session_slot_id
        assert (p1.exercise_slot.order, p0.exercise_slot.order) == (0, 1)
        # session_b is untouched.
        assert q0.exercise_slot.session_slot_id == session_b.session_slot_id
        assert q0.exercise_slot.order == 0

    def test_cross_week_move_400(self, client):
        link, plan, week1, week2 = _two_week_plan()
        cell1 = list(week1.sessions.first().cells())[0]
        session2 = week2.sessions.first()
        client.force_login(link.coach)
        resp = post_json(
            client, _move_url(plan, cell1), {"session_id": session2.pk, "index": 0}
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "Move within one week."
        cell1.refresh_from_db()
        assert (
            cell1.exercise_slot.session_slot_id
            == week1.sessions.first().session_slot_id
        )

    def test_index_clamps_above_range_to_the_end(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": session_b.pk, "index": 999}
        )
        assert resp.status_code == 200
        p0.refresh_from_db()
        q0.refresh_from_db()
        assert p0.exercise_slot.session_slot_id == session_b.session_slot_id
        assert p0.exercise_slot.order == 1  # clamped to the end (after q0)
        assert q0.exercise_slot.order == 0

    def test_index_clamps_below_range_to_zero(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": session_b.pk, "index": -5}
        )
        assert resp.status_code == 200
        p0.refresh_from_db()
        q0.refresh_from_db()
        assert p0.exercise_slot.session_slot_id == session_b.session_slot_id
        assert p0.exercise_slot.order == 0
        assert q0.exercise_slot.order == 1

    def test_missing_session_id_400(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(client, _move_url(plan, p0), {"index": 0})
        assert resp.status_code == 400

    def test_non_int_session_id_400(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(client, _move_url(plan, p0), {"session_id": "abc", "index": 0})
        assert resp.status_code == 400

    def test_missing_index_400(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(client, _move_url(plan, p0), {"session_id": session_b.pk})
        assert resp.status_code == 400

    def test_non_int_index_400(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            _move_url(plan, p0),
            {"session_id": session_b.pk, "index": "abc"},
        )
        assert resp.status_code == 400

    def test_malformed_json_400(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            _move_url(plan, p0), data="not json", content_type="application/json"
        )
        assert resp.status_code == 400

    def test_foreign_session_id_400(self, client):
        # A decision where the spec is silent: a session_id that doesn't
        # resolve to a live session of THIS plan is a 400 (a body-referenced
        # id failing validation), mirroring prescription_override's
        # _group_member_or_none 400-on-bad-reference convention — not the
        # URL-segment 404 convention used for the endpoint's own `pk`.
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": 999999, "index": 0}
        )
        assert resp.status_code == 400

    def test_soft_deleted_target_session_id_400(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        del_resp = client.post(
            reverse(
                "meso:api_session_delete",
                kwargs={"plan_id": plan.pk, "pk": session_b.pk},
            )
        )
        assert del_resp.status_code == 200
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": session_b.pk, "index": 0}
        )
        assert resp.status_code == 400

    def test_requires_login(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": session_b.pk, "index": 0}
        )
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = client.get(_move_url(plan, p0))
        assert resp.status_code == 405

    def test_non_owner_forbidden(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(UserFactory())
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": session_b.pk, "index": 0}
        )
        assert resp.status_code == 403

    def test_unknown_prescription_pk_404(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client,
            reverse(
                "meso:api_prescription_move",
                kwargs={"plan_id": plan.pk, "pk": 999999},
            ),
            {"session_id": session_b.pk, "index": 0},
        )
        assert resp.status_code == 404

    def test_soft_deleted_prescription_404(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        del_resp = client.post(
            reverse(
                "meso:api_prescription_delete", kwargs={"plan_id": plan.pk, "pk": p0.pk}
            )
        )
        assert del_resp.status_code == 200
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": session_b.pk, "index": 0}
        )
        assert resp.status_code == 404

    def test_soft_deleted_ancestor_week_404(self, client):
        link, plan, week1, week2 = _two_week_plan()
        session2a = week2.sessions.first()
        session2b = day(week2, day_number=2, name="Upper", order=1)
        cell2 = list(session2a.cells())[0]
        client.force_login(link.coach)
        del_resp = client.post(
            reverse(
                "meso:api_week_delete", kwargs={"plan_id": plan.pk, "week_id": week2.pk}
            )
        )
        assert del_resp.status_code == 200
        resp = post_json(
            client, _move_url(plan, cell2), {"session_id": session2b.pk, "index": 0}
        )
        assert resp.status_code == 404

    def test_records_exactly_one_plan_action_with_loosely_pinned_label(self, client):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": session_b.pk, "index": 0}
        )
        assert resp.status_code == 200
        actions = list(undo_actions(plan))
        assert len(actions) == 1
        assert "Moved" in actions[0].label
        assert "Box Squat" in actions[0].label

    def test_undo_round_trip_puts_the_row_back_in_source_session_at_old_order(
        self, client
    ):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": session_b.pk, "index": 0}
        )
        assert resp.status_code == 200

        resp = client.post(undo_url(plan))
        assert resp.status_code == 200
        p0.refresh_from_db()
        p1.refresh_from_db()
        q0.refresh_from_db()
        assert p0.exercise_slot.session_slot_id == session_a.session_slot_id
        assert p0.exercise_slot.order == 0
        assert p1.exercise_slot.session_slot_id == session_a.session_slot_id
        assert p1.exercise_slot.order == 1
        assert q0.exercise_slot.session_slot_id == session_b.session_slot_id
        assert q0.exercise_slot.order == 0

    def test_logged_set_survives_the_move_pointing_at_the_same_prescription(
        self, client
    ):
        plan, week, session_a, session_b, p0, p1, q0 = seed_two_sessions()
        log = SessionLogFactory(session=session_a, athlete=plan.athlete)
        logged_set = LoggedSetFactory(session_log=log, prescription=p0)
        client.force_login(plan.relationship.coach)
        resp = post_json(
            client, _move_url(plan, p0), {"session_id": session_b.pk, "index": 0}
        )
        assert resp.status_code == 200

        logged_set.refresh_from_db()
        assert logged_set.prescription_id == p0.pk
        assert logged_set.session_log_id == log.pk
        # The log itself keeps pointing at the (untouched) session it was
        # logged against — moving the prescription never touches SessionLog.
        log.refresh_from_db()
        assert log.session_id == session_a.pk
