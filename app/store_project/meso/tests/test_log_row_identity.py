"""Which log backs a line: the blur response and the next render must agree (#568).

This file used to also pin the #567 row-identity contract of the structured
Set-row logger (``id`` / ``client_id`` on posted sets, positional fallback,
twin absorb). That logger is retired (#578 stage 4): ``athlete_log_session``
no longer accepts sets, so those tests were deleted with it. What remains is
about surviving code -- ``sub_line_warn_reason`` and the cell-write response
against the presenter -- and never touched the log endpoint.
"""

import datetime
import json

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso import views as meso_views
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


# -- #568: the blur response and the next render must agree ------------------


class TestSubLineWarnAgreesAcrossSurfaces:
    """The blur response and the next render must agree on a line's tint.

    ``sub_line_warn_reason``'s fallback used to match a ``LoggedSet`` on ANY
    ``SessionLog`` in the database, while the presenter always reads one
    specific log. The two ways they can diverge, per the issue: a stray
    second log for the same (session, athlete), and a coach move that takes
    the cell to another day while the ``LoggedSet`` stays behind.
    """

    def test_a_second_log_for_the_same_session_athlete_does_not_back_the_line(
        self, client
    ):
        s = seed()
        # A coach-authored sub-line the athlete never touched, so a blur that
        # re-posts its own unchanged text is a no-op (`untouched_coach_line`)
        # and never re-derives a fresh backing row -- the only way to observe
        # `_cell_warn_reason_or_blank`'s read without it healing the very gap this
        # test means to catch.
        cell = sub_line(s.squat, "225 x 5", line=1)
        old_log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, date=timezone.localdate()
        )
        LoggedSet.objects.create(
            session_log=old_log,
            prescription=s.squat,
            source_line=cell,
            set_number=1,
            reps="5",
            load="225",
            rpe="",
        )
        SessionLog.objects.filter(pk=old_log.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=1)
        )
        # The newest log for this (session, athlete) -- the one the presenter
        # and (once fixed) the blur response both read -- has NO sets at all.
        SessionLog.objects.create(
            session=s.session, athlete=s.athlete, date=timezone.localdate()
        )

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        blur_warn = resp.json()["cell"]["warn"]

        ctx = presenters.athlete_session(s.session, s.athlete)
        squat_ctx = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        render_warn = next(
            line["warn"] for line in squat_ctx["sub_lines"] if line["line"] == 1
        )

        assert blur_warn == render_warn is True, (
            "the newest log has no set backing this line -- both surfaces "
            f"must call it unlogged/tinted (blur={blur_warn}, render={render_warn})"
        )

    def test_a_moved_exercise_reads_unlogged_on_its_new_day(self, client):
        """After a move, the cell travels but the ``LoggedSet`` doesn't.

        ``prescription_move`` moves the ``ExerciseSlot`` to the new day; the
        ``LoggedSet`` stays on the old day's log. The DECIDED answer (#568)
        is that the line now reads unlogged on the new day -- a behavior
        change for ordinary parsed rows, taken deliberately rather than left
        to the two surfaces to disagree about.
        """
        s = seed()
        day2 = day(s.week, day_number=2, name="Upper", bias="Push")
        cell = sub_line(s.squat, "225 x 5", line=1)
        old_log = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, date=timezone.localdate()
        )
        LoggedSet.objects.create(
            session_log=old_log,
            prescription=s.squat,
            source_line=cell,
            set_number=1,
            reps="5",
            load="225",
            rpe="",
        )

        client.force_login(s.coach)
        resp = client.post(
            reverse(
                "meso:api_prescription_move",
                kwargs={"plan_id": s.plan.pk, "pk": s.squat.pk},
            ),
            data=json.dumps({"session_id": day2.pk, "index": 0}),
            content_type="application/json",
        )
        assert resp.status_code == 200

        client.force_login(s.athlete)
        resp = write_cell(client, day2, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        blur_warn = resp.json()["cell"]["warn"]

        ctx = presenters.athlete_session(day2, s.athlete)
        squat_ctx = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        render_warn = next(
            line["warn"] for line in squat_ctx["sub_lines"] if line["line"] == 1
        )

        assert blur_warn == render_warn is True, (
            "the old day's set must not back the line on its new day -- both "
            f"surfaces must read unlogged (blur={blur_warn}, render={render_warn})"
        )


# -- adversarial review round: P1-A/P1-B/P2-A/P2-B ---------------------------


class TestCellWarnAgreesWithAFreshSkipRead:
    """#567/#568 P1-H: ``loggable`` must come from a FRESH read, not a stale snapshot.

    ``athlete_cell_write`` builds ``line_zero`` (the exercise's line-0 cell)
    BEFORE the write transaction. ``_upsert_parsed_set`` re-reads it under
    ``select_for_update`` and acts on THAT fresh value. If
    ``_cell_warn_reason_or_blank`` instead reads the caller's stale pre-transaction
    instance, the two disagree the moment a coach's ``prescription_unskip``
    lands inside this same request's window: the request logs a REAL set
    (fresh: unskipped) but the response reports ``warn=True`` from the stale
    (skipped) snapshot -- contradicting the very set it just wrote, and a
    live counterexample to the "one answer for the tint" invariant #568
    exists to guarantee.
    """

    def test_warn_agrees_when_skip_is_lifted_mid_request(self, client, monkeypatch):
        s = seed()
        s.squat.skipped = True
        s.squat.save(update_fields=["skipped"])
        client.force_login(s.athlete)

        real_upsert = meso_views._upsert_parsed_set

        def unskip_then_upsert(session, athlete, line_zero_cell, cell, **kwargs):
            # Simulates a coach's `prescription_unskip` landing INSIDE this
            # request's window: after `athlete_cell_write` already snapshotted
            # `line_zero` (stale: skipped=True) but before the fresh, locked
            # re-read `_upsert_parsed_set` itself takes.
            Prescription.objects.filter(pk=s.squat.pk).update(skipped=False)
            return real_upsert(session, athlete, line_zero_cell, cell, **kwargs)

        monkeypatch.setattr(meso_views, "_upsert_parsed_set", unskip_then_upsert)

        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        blur_warn = resp.json()["cell"]["warn"]

        cell = sub_cell(s.squat, 1)
        assert LoggedSet.objects.filter(source_line=cell).exists(), (
            "the fresh (unskipped) read must have let this request log a real set"
        )

        ctx = presenters.athlete_session(s.session, s.athlete)
        squat_ctx = next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)
        render_warn = next(
            line["warn"] for line in squat_ctx["sub_lines"] if line["line"] == 1
        )

        assert blur_warn == render_warn is False, (
            "a set really was logged this request (fresh unskip) -- the "
            "response must agree with the next render, not the caller's "
            f"stale skipped snapshot (blur={blur_warn}, render={render_warn})"
        )
