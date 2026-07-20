"""Athlete slice Phase 4a — freeform sub-line tracking (the athlete's write).

The athlete's delivered session gains an *editable* sub-line stack beneath each
exercise: ``POST /meso/api/me/session/<id>/cell/`` upserts a line>=1
``Prescription`` cell (the same (slot × week × line) address the coach's
``cell_line_write`` uses) and stamps it ``athlete_authored=True`` — the flag
that keeps these cells out of the coach's undo/redo snapshot machinery (a
coach undo must never clobber or hard-delete an athlete's tracking note).

The endpoint mirrors ``athlete_log_session``'s discipline: athlete-scoped
(only a session they own through an active coach link), every out-of-scope
target a flat 404, bad input a 400 that writes nothing, and the write is an
idempotent upsert (re-writing the same cell updates the one row). It records
NO coach ``PlanAction``. The undo-interaction block pins the ``athlete_authored`` isolation:
snapshots omit athlete cells, and restore never overwrites/deletes them — even
when an older snapshot still holds a coach version of that same pk.
"""

import json
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.history import serialize_plan_snapshot
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import PlanAction
from store_project.meso.models import Prescription
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.meso.tests._helpers import sub_line
from store_project.meso.views import MAX_CELL_LINE
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed(
    *,
    coach=None,
    athlete=None,
    delivered=True,
    link_status=CoachAthlete.Status.ACTIVE,
    plan_status=Plan.Status.ACTIVE,
):
    """A minimal plan → (optionally delivered) week → session → two line-0 cells."""
    coach = coach or UserFactory()
    athlete = athlete or UserFactory()
    rel = CoachAthleteFactory(coach=coach, athlete=athlete, status=link_status)
    plan = PlanFactory(relationship=rel, title="Hypertrophy Block", status=plan_status)
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(
        mesocycle=meso,
        index=2,
        delivered_at=timezone.now() if delivered else None,
    )
    session = day(week, day_number=1, name="Lower", bias="Quad")
    squat = presc(
        session, name="Box Squat", order=0, sets="3", reps="6", load="70", rpe="7"
    )
    rdl = presc(session, name="RDL", order=1, sets="3", reps="8", load="80", rpe="8")
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        rel=rel,
        plan=plan,
        meso=meso,
        week=week,
        session=session,
        squat=squat,
        rdl=rdl,
    )


def cell_url(session):
    return reverse("meso:athlete_cell_write", kwargs={"pk": session.pk})


def post(client, session, payload):
    return client.post(
        cell_url(session),
        data=json.dumps(payload),
        content_type="application/json",
    )


def sub_cells(cell):
    """The line>=1 cells beneath a line-0 cell's row, for its week."""
    return Prescription.objects.filter(
        exercise_slot=cell.exercise_slot, week=cell.week, line__gte=1
    )


# -- write semantics -------------------------------------------------------


class TestSubLineWrite:
    def test_athlete_writes_sub_line_creates_cell(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {"exercise_id": s.squat.pk, "line": 1, "text": "felt heavy"},
        )
        assert resp.status_code == 200
        cell = Prescription.objects.get(
            exercise_slot=s.squat.exercise_slot, week=s.session.week, line=1
        )
        assert cell.text == "felt heavy"
        assert cell.athlete_authored is True

        data = resp.json()
        assert data["ok"] is True
        assert data["cell"]["id"] == cell.pk
        assert data["cell"]["exercise_slot_id"] == s.squat.exercise_slot_id
        assert data["cell"]["week_id"] == s.session.week_id
        assert data["cell"]["line"] == 1
        assert data["cell"]["text"] == "felt heavy"

    def test_athlete_sub_line_upsert_updates_not_duplicates(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert (
            post(
                client,
                s.session,
                {"exercise_id": s.squat.pk, "line": 1, "text": "first"},
            ).status_code
            == 200
        )
        assert (
            post(
                client,
                s.session,
                {"exercise_id": s.squat.pk, "line": 1, "text": "second"},
            ).status_code
            == 200
        )
        # One row for (slot, week, line=1), updated in place.
        assert sub_cells(s.squat).count() == 1
        assert sub_cells(s.squat).get().text == "second"

    def test_athlete_sub_line_blank_text_clears(self, client):
        s = seed()
        client.force_login(s.athlete)
        post(client, s.session, {"exercise_id": s.squat.pk, "line": 1, "text": "note"})
        # Clearing blanks the cell in place — never a delete (spreadsheet semantics).
        assert (
            post(
                client, s.session, {"exercise_id": s.squat.pk, "line": 1, "text": ""}
            ).status_code
            == 200
        )
        assert sub_cells(s.squat).count() == 1
        assert sub_cells(s.squat).get().text == ""

        # The presenter drops the blank cell from the athlete's display stack.
        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        assert row["sub_lines"] == []

    def test_athlete_cannot_write_line_zero(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post(
            client, s.session, {"exercise_id": s.squat.pk, "line": 0, "text": "hijack"}
        )
        assert resp.status_code == 400
        s.squat.refresh_from_db()
        # The coach's prescription line is untouched.
        assert s.squat.text == "3 x 6, RPE 7, 70"

    def test_athlete_never_sets_skipped(self, client):
        s = seed()
        client.force_login(s.athlete)
        # A stray ``skipped`` in the body is ignored — the endpoint reads only
        # exercise_id/line/text.
        resp = post(
            client,
            s.session,
            {"exercise_id": s.squat.pk, "line": 1, "text": "x", "skipped": True},
        )
        assert resp.status_code == 200
        s.squat.refresh_from_db()
        assert s.squat.skipped is False  # line-0 skip untouched
        assert sub_cells(s.squat).get().skipped is False

    def test_athlete_sub_line_rejects_line_over_max(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {"exercise_id": s.squat.pk, "line": MAX_CELL_LINE + 1, "text": "too far"},
        )
        assert resp.status_code == 400
        assert sub_cells(s.squat).count() == 0

    def test_athlete_sub_line_bad_body_400(self, client):
        s = seed()
        client.force_login(s.athlete)
        bad_bodies = [
            {"exercise_id": s.squat.pk, "line": "x", "text": "note"},  # line not int
            {"exercise_id": "y", "line": 1, "text": "note"},  # exercise_id not int
            {"exercise_id": s.squat.pk, "line": 1, "text": 5},  # text not str
        ]
        for body in bad_bodies:
            assert post(client, s.session, body).status_code == 400
        # Malformed JSON is a 400 too.
        resp = client.post(
            cell_url(s.session), data="not json", content_type="application/json"
        )
        assert resp.status_code == 400
        # Nothing was written by any of the rejected requests.
        assert sub_cells(s.squat).count() == 0


# -- scoping (parity with ``_athlete_session_or_404``) ---------------------


class TestSubLineScoping:
    def test_athlete_sub_line_foreign_session_404(self, client):
        s = seed()
        intruder = seed().athlete
        client.force_login(intruder)
        resp = post(
            client, s.session, {"exercise_id": s.squat.pk, "line": 1, "text": "x"}
        )
        assert resp.status_code == 404
        assert sub_cells(s.squat).count() == 0

    def test_athlete_sub_line_archived_plan_404(self, client):
        s = seed(plan_status=Plan.Status.ARCHIVED)
        client.force_login(s.athlete)
        resp = post(
            client, s.session, {"exercise_id": s.squat.pk, "line": 1, "text": "x"}
        )
        assert resp.status_code == 404

    def test_athlete_sub_line_unknown_session_404(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = client.post(
            reverse("meso:athlete_cell_write", kwargs={"pk": 999999}),
            data=json.dumps({"exercise_id": s.squat.pk, "line": 1, "text": "x"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_athlete_sub_line_exercise_not_in_session_400(self, client):
        s = seed()
        other = seed()  # an unrelated plan/session/line-0 cell
        client.force_login(s.athlete)
        resp = post(
            client, s.session, {"exercise_id": other.squat.pk, "line": 1, "text": "x"}
        )
        assert resp.status_code == 400
        assert sub_cells(other.squat).count() == 0

    def test_athlete_sub_line_no_billing_gate(self, client):
        # A soft-suspended (over-seat-limit) coach relationship freezes the
        # COACH's editing, not the athlete's own tracking. Mirror the designer's
        # over-limit setup: an older kept link plus this plan's newer one pushes
        # the free coach over the cap — yet the athlete still writes.
        coach = UserFactory()
        CoachAthleteFactory(coach=coach)  # older link → kept
        s = seed(coach=coach)  # newer → the coach side is suspended
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {"exercise_id": s.squat.pk, "line": 1, "text": "still logging"},
        )
        assert resp.status_code == 200
        assert sub_cells(s.squat).get().text == "still logging"


# -- current-week advance (parity with ``athlete_log_session``) ------------


# -- undo interaction: athlete cells are invisible to coach undo/redo -------


def undo_url(plan):
    return reverse("meso:api_plan_undo", kwargs={"plan_id": plan.pk})


def redo_url(plan):
    return reverse("meso:api_plan_redo", kwargs={"plan_id": plan.pk})


def coach_patch(client, plan, cell, text):
    resp = client.post(
        reverse(
            "meso:api_prescription_patch", kwargs={"plan_id": plan.pk, "pk": cell.pk}
        ),
        data=json.dumps({"text": text}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    return resp


def coach_cell_write(client, plan, slot_id, week_id, line, text):
    resp = client.post(
        reverse(
            "meso:api_cell_line_write", kwargs={"plan_id": plan.pk, "slot_id": slot_id}
        ),
        data=json.dumps({"week_id": week_id, "line": line, "text": text}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    return resp


class TestSubLineUndoIsolation:
    def test_athlete_sub_line_records_no_plan_action(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert (
            post(
                client, s.session, {"exercise_id": s.squat.pk, "line": 1, "text": "x"}
            ).status_code
            == 200
        )
        # The write is athlete-initiated — it never enters the coach's undo stack.
        assert PlanAction.objects.filter(plan=s.plan).count() == 0

    def test_athlete_cell_excluded_from_snapshot(self, client):
        s = seed()
        client.force_login(s.athlete)
        post(client, s.session, {"exercise_id": s.squat.pk, "line": 1, "text": "mine"})
        sub = sub_cells(s.squat).get()

        snapshot = serialize_plan_snapshot(s.plan)
        snap_cell_pks = {c["pk"] for c in snapshot["cells"]}
        # The athlete-authored cell is invisible to the snapshot machinery...
        assert sub.pk not in snap_cell_pks
        # ...while the coach's own line-0 cell is captured as usual.
        assert s.squat.pk in snap_cell_pks

    def test_coach_undo_preserves_athlete_sub_line(self, client):
        s = seed()
        # Coach edit E1 (records one undo action, snapshot pre-dates W).
        client.force_login(s.coach)
        coach_patch(client, s.plan, s.squat, "3 x 6, RPE 8, 75")
        # Athlete writes W after E1.
        client.force_login(s.athlete)
        post(
            client,
            s.session,
            {"exercise_id": s.squat.pk, "line": 1, "text": "felt strong"},
        )
        w = sub_cells(s.squat).get()
        # Coach undoes E1 — its snapshot never accounted for W, but W survives.
        client.force_login(s.coach)
        assert client.post(undo_url(s.plan)).status_code == 200

        s.squat.refresh_from_db()
        assert s.squat.text == "3 x 6, RPE 7, 70"  # the coach edit reverted
        w.refresh_from_db()
        assert w.text == "felt strong"  # the athlete note is untouched
        assert w.athlete_authored is True

    def test_coach_redo_preserves_athlete_sub_line(self, client):
        s = seed()
        client.force_login(s.coach)
        coach_patch(client, s.plan, s.squat, "3 x 6, RPE 8, 75")
        client.force_login(s.athlete)
        post(
            client,
            s.session,
            {"exercise_id": s.squat.pk, "line": 1, "text": "felt strong"},
        )
        w = sub_cells(s.squat).get()

        client.force_login(s.coach)
        assert client.post(undo_url(s.plan)).status_code == 200
        assert client.post(redo_url(s.plan)).status_code == 200

        s.squat.refresh_from_db()
        assert s.squat.text == "3 x 6, RPE 8, 75"  # the coach edit is back
        w.refresh_from_db()
        assert w.text == "felt strong"  # the athlete note rode through both
        assert w.athlete_authored is True

    def test_coach_undo_does_not_overwrite_athlete_edit_to_coach_line(self, client):
        # The half-fix trap: a capture-only exclusion still lets an OLDER coach
        # snapshot overwrite an athlete edit on restore. The line-1 pk was
        # coach-authored when snapshot S2 was taken, so restore must skip it by
        # the CURRENT DB row's flag (now athlete's), not the snapshot's.
        s = seed()
        client.force_login(s.coach)
        # A1 creates a coach line-1 cell.
        coach_cell_write(
            client, s.plan, s.squat.exercise_slot_id, s.session.week_id, 1, "RPE 8"
        )
        # A2's snapshot captures the coach's line-1 = "RPE 8".
        coach_patch(client, s.plan, s.squat, "3 x 6, RPE 7, 99")
        # The athlete edits that same line-1 cell — flips it athlete-authored.
        client.force_login(s.athlete)
        post(
            client,
            s.session,
            {"exercise_id": s.squat.pk, "line": 1, "text": "athlete note"},
        )
        line1 = sub_cells(s.squat).get()
        # Coach undoes A2 (whose snapshot still holds the coach "RPE 8").
        client.force_login(s.coach)
        assert client.post(undo_url(s.plan)).status_code == 200

        line1.refresh_from_db()
        assert line1.text == "athlete note"  # the athlete edit is NOT clobbered
        assert line1.athlete_authored is True
        s.squat.refresh_from_db()
        assert s.squat.text == "3 x 6, RPE 7, 70"  # line-0 reverted by the undo

    def test_coach_reclaims_cell_via_cell_line_write(self, client):
        s = seed()
        # The athlete authors line-1...
        client.force_login(s.athlete)
        post(client, s.session, {"exercise_id": s.squat.pk, "line": 1, "text": "mine"})
        assert sub_cells(s.squat).get().athlete_authored is True
        # ...then the coach edits the same cell — reclaiming it into coach history.
        client.force_login(s.coach)
        coach_cell_write(
            client, s.plan, s.squat.exercise_slot_id, s.session.week_id, 1, "coach"
        )

        cell = sub_cells(s.squat).get()
        assert cell.text == "coach"
        assert cell.athlete_authored is False

    def test_coach_undo_after_reclaim_restores_athlete_text(self, client):
        # Reclaim-then-snapshot: when a coach edits an EXISTING athlete-authored
        # cell, the athlete's original text must survive a coach undo (restored
        # as a coach-owned cell), never be hard-deleted.
        s = seed()
        # Athlete authors line-1 = "mine".
        client.force_login(s.athlete)
        post(client, s.session, {"exercise_id": s.squat.pk, "line": 1, "text": "mine"})
        assert sub_cells(s.squat).get().athlete_authored is True
        # Coach edits the same cell to "coach" — reclaims it into coach history.
        client.force_login(s.coach)
        coach_cell_write(
            client, s.plan, s.squat.exercise_slot_id, s.session.week_id, 1, "coach"
        )
        cell_pk = sub_cells(s.squat).get().pk

        # Coach undo restores the athlete's original text (as a coach cell) —
        # the row still EXISTS, not hard-deleted.
        assert client.post(undo_url(s.plan)).status_code == 200
        cell = Prescription.objects.get(pk=cell_pk)
        assert cell.text == "mine"
        assert cell.athlete_authored is False

        # Coach redo reapplies the coach edit.
        assert client.post(redo_url(s.plan)).status_code == 200
        cell.refresh_from_db()
        assert cell.text == "coach"
        assert cell.athlete_authored is False


# -- presenter -------------------------------------------------------------


class TestSubLinePresenter:
    def test_athlete_session_exposes_editable_sub_lines(self, client):
        s = seed()
        sub_line(s.squat, "RPE 8")  # a line-1 cell beneath the squat row
        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        # "RPE 8" isn't a set attempt (5a §8) — warn is False.
        assert row["sub_lines"] == [{"line": 1, "text": "RPE 8", "warn": False}]

        payload = presenters.athlete_log_payload(ctx)
        assert payload["cell_url"] == cell_url(s.session)
        pr = next(e for e in payload["exercises"] if e["id"] == s.squat.pk)
        assert pr["sub_lines"] == [{"line": 1, "text": "RPE 8", "warn": False}]

    def test_athlete_session_sub_lines_warn_only_on_unresolved_set(self, client):
        # 5a §8: derive-on-read warn — only the `unresolved-set` classification
        # (a fat-fingered set attempt) warns; a resolvable set does not.
        s = seed()
        sub_line(s.squat, "225 x", line=1)  # unresolved-set — warns
        sub_line(s.squat, "225 x 5", line=2)  # a real set — no warn
        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        warn_by_line = {entry["line"]: entry["warn"] for entry in row["sub_lines"]}
        assert warn_by_line == {1: True, 2: False}

    def test_athlete_session_set_rows_exclude_parsed_sets(self, client):
        # No double-display (5a §6): a LoggedSet derived from a sub-line
        # (source_line set) must render only as that sub-line's text, never
        # also as a phantom structured set-input row.
        from store_project.meso.factories import LoggedSetFactory
        from store_project.meso.factories import SessionLogFactory
        from store_project.meso.models import SessionLog

        s = seed()
        # athlete_authored=True is load-bearing: the suppression keys on that
        # flag (a reclaimed line stops suppressing), and it is the only state
        # `athlete_cell_write` can produce.
        cell = sub_line(s.squat, "225 x 5", athlete_authored=True)
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )
        LoggedSetFactory(
            session_log=log,
            prescription=s.squat,
            source_line=cell,
            set_number=1,
            reps="5",
            load="225",
            # rpe="" because "225 x 5" carries none. The upsert always writes
            # exactly what the text parsed to, so the factory's default rpe="7"
            # would model a set the app can't produce — and suppression asks
            # whether the text still shows THIS performance.
            rpe="",
        )
        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        # The parsed set shows once, as sub_lines text...
        assert row["sub_lines"] == [{"line": 1, "text": "225 x 5", "warn": False}]
        # ...and never a second time as a filled/"done" structured set row.
        assert all(not r["done"] for r in row["set_rows"])
        assert all(r["reps"] == "" and r["load"] == "" for r in row["set_rows"])

    def test_athlete_session_set_rows_include_structured_sets(self, client):
        # A structured-logger set (source_line NULL) still hydrates set_rows —
        # only the parsed-derivative channel is excluded.
        from store_project.meso.factories import LoggedSetFactory
        from store_project.meso.factories import SessionLogFactory
        from store_project.meso.models import SessionLog

        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        LoggedSetFactory(
            session_log=log,
            prescription=s.squat,
            source_line=None,
            set_number=1,
            reps="6",
            load="70",
        )
        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        done_rows = [r for r in row["set_rows"] if r["done"]]
        assert len(done_rows) == 1
        assert done_rows[0]["reps"] == "6"
        assert done_rows[0]["load"] == "70"

    def test_athlete_session_target_is_prescription_only(self, client):
        # The now-editable sub-line stack must not double-display inside the
        # read-only target string — ``target`` folds LINE 0 only.
        s = seed()
        sub_line(s.squat, "RPE 9")
        ctx = presenters.athlete_session(s.session, s.athlete)
        row = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        assert row["target"] == "3 x 6, RPE 7, 70"
