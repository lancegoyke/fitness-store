"""A coach undo must not detach the athlete's own performed set (#577).

``restore_plan_snapshot``'s stray-cell purge spares a cell athlete data points
at — but for two of the three pointers only: ``parsed_sets``
(``LoggedSet.source_line``) and ``reclaimed_sets`` (``LoggedSet.reclaimed_line``,
#541). ``logged_sets`` (``LoggedSet.prescription``, ``SET_NULL``) — the most
direct of the three, the line-0 cell every logged set is filed under — was left
out, so an undo could hard-delete a cell an ordinary structured row names. The
row survived with ``prescription = NULL``, and every derivation filters that
out: the set stayed on the athlete's page as text while silently ceasing to
count toward their estimated 1RM and their records.

ON REACHING THE PRECONDITION. The purge only fires on a cell absent from the
snapshot whose slot *and* week are both live in it. No real endpoint produces
that for a line-0 cell: every path that creates an ``ExerciseSlot``
(``_new_block_wide_row``, ``session_add``, ``Mesocycle.scaffold``,
``append_week`` — including its ``source is None`` fallback to ``{0: ""}`` — and
the agent's ``_apply_add``) creates the line-0 cell for every live week in the
SAME transaction, and the purge below is the only hard-delete of a
``Prescription`` in the whole codebase. Undo/redo's strict LIFO can't
manufacture the gap either: reviving an older row means first undoing every
later action, which removes whatever that action created. So the one step below
that does NOT go through an endpoint is creating a bare ``ExerciseSlot`` with no
cells; the lazy line-0 create, the athlete's logged set and the coach's undo are
all real requests. The gap is latent today, which is why the test has to stand
it up — but the promise the purge already makes ("a cell some athlete data
POINTS AT is the same promise one join away") is an invariant, not a scenario,
and ``prescription`` is the pointer it costs the most to break.
"""

import json

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import history
from store_project.meso import one_rm
from store_project.meso import personal_records
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import PlanAction
from store_project.meso.models import Prescription
from store_project.meso.tests._helpers import day
from store_project.meso.tests.test_parse_at_commit import reclaim
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _seed_slot_without_cells():
    """A delivered week whose one exercise row has NO ``Prescription`` yet.

    The artificial step (see the module docstring): ``ExerciseSlot`` straight
    through the ORM, skipping the bundled cell creation every real path does.
    """
    coach = UserFactory()
    athlete = UserFactory()
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(relationship=rel, title="Strength", status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, name="Block 1", order=0)
    week = WeekFactory(mesocycle=meso, index=1, delivered_at=timezone.now())
    session = day(week, day_number=1, name="Upper", bias="")
    slot = ExerciseSlot.objects.create(
        session_slot=session.session_slot, name="Bench Press", order=1
    )
    assert not Prescription.objects.filter(exercise_slot=slot).exists()
    return coach, athlete, plan, week, session, slot


def _write_line(client, plan, slot, week, *, line, text):
    return client.post(
        reverse(
            "meso:api_cell_line_write",
            kwargs={"plan_id": plan.pk, "slot_id": slot.pk},
        ),
        data=json.dumps({"week_id": week.pk, "line": line, "text": text}),
        content_type="application/json",
    )


def _log_one_set(client, session, cell):
    return client.post(
        reverse("meso:athlete_log_session", kwargs={"pk": session.pk}),
        data=json.dumps(
            {
                "status": "done",
                "sets": [
                    {
                        "prescription": cell.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "225",
                        "rpe": "8",
                    }
                ],
            }
        ),
        content_type="application/json",
    )


def _undo(client, plan):
    return client.post(
        reverse("meso:api_plan_undo", kwargs={"plan_id": plan.pk}),
        content_type="application/json",
    )


class TestUndoSparesACellALoggedSetPointsAt:
    def test_the_set_still_counts_toward_1rm_and_prs_after_an_undo(self, client):
        coach, athlete, plan, week, session, slot = _seed_slot_without_cells()

        # Slot and week both live, no cell yet — exactly what
        # ``record_plan_action`` captures if a coach edit fires now.
        before_the_cell = history.serialize_plan_snapshot(plan)

        client.force_login(coach)
        resp = _write_line(client, plan, slot, week, line=0, text="3 x 5, 225")
        assert resp.status_code == 200, resp.content
        cell = Prescription.objects.get(exercise_slot=slot, week=week, line=0)

        client.force_login(athlete)
        assert _log_one_set(client, session, cell).status_code == 200
        row = LoggedSet.objects.get(
            session_log__session=session, session_log__athlete=athlete
        )
        assert row.prescription_id == cell.pk

        key = one_rm.key_str(slot.exercise_id, slot.name)
        assert key in one_rm.derive_one_rm_values(athlete, unit=plan.unit)
        assert key in personal_records.personal_records(athlete, unit=plan.unit)

        # The undo restores the snapshot the endpoint took for itself. Assert
        # it IS the one above, so a future refactor that moves
        # ``record_plan_action`` fails here rather than quietly testing
        # something else.
        recorded = PlanAction.objects.filter(
            plan=plan, stack=PlanAction.Stack.UNDO
        ).latest("seq")
        assert recorded.snapshot == before_the_cell

        client.force_login(coach)
        assert _undo(client, plan).status_code == 200

        row.refresh_from_db()
        assert row.prescription_id == cell.pk, (
            "undo hard-deleted the cell the athlete's logged set points at"
        )
        assert key in one_rm.derive_one_rm_values(athlete, unit=plan.unit), (
            "the set stopped counting toward the athlete's estimated 1RM"
        )
        assert key in personal_records.personal_records(athlete, unit=plan.unit), (
            "the set stopped counting toward the athlete's records"
        )

    def test_a_cell_nothing_points_at_is_still_purged(self, client):
        """The exclusion must spare pointed-at cells, not stop purging."""
        coach, athlete, plan, week, session, slot = _seed_slot_without_cells()
        before_the_cell = history.serialize_plan_snapshot(plan)

        client.force_login(coach)
        assert (
            _write_line(client, plan, slot, week, line=0, text="3 x 5").status_code
            == 200
        )
        stray = Prescription.objects.get(exercise_slot=slot, week=week, line=0)
        assert not LoggedSet.objects.filter(prescription=stray).exists()

        history.restore_plan_snapshot(plan, before_the_cell)

        assert not Prescription.objects.filter(pk=stray.pk).exists(), (
            "a stray cell no athlete data points at must still be purged"
        )


class TestRestoreAfterReclaimSparesANullPrescriptionRow:
    """Retyping a reclaimed line's original text must not adopt a broken row.

    ``_upsert_parsed_set``'s ``existing`` reuse lookup (the direct
    restore-after-reclaim case, not the ``reclaimed_line`` fallback #541
    added) used to match a survivor by ``source_line=cell`` alone: the
    athlete types a set, the coach reclaims the sub-line (overwriting its
    text, but the row keeps its ``source_line`` — see
    ``TestReclaimLeavesAthleteDataAlone`` in test_parse_at_commit.py), then
    the athlete types the identical text back onto the line. ``mine`` is
    empty (the coach's cue no longer describes the old row), so the code
    falls through to ``existing`` and reuses it rather than minting a twin —
    ordinarily the right call.

    But a row #577 already damaged (its line-0 cell purged out from under it,
    ``prescription`` gone ``NULL`` via ``SET_NULL``) still matches
    ``source_line=cell`` too, and reusing IT re-links the line to a set every
    derivation (``one_rm``, ``personal_records``, ``settle``) filters out —
    an inert, invisible "restore" instead of a fresh, countable set. Scoping
    ``existing`` by ``prescription=line_zero_cell`` (matching the ``mine``
    delete above it and the ``reclaimed_line`` fallback below it) closes
    that: a NULL-prescription row can no longer satisfy either lookup, so the
    upsert falls through to CREATE and mints a live row instead.
    """

    def test_the_restore_mints_a_fresh_row_not_the_null_prescription_survivor(
        self, client
    ):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        original = LoggedSet.objects.get(source_line=cell)

        client.force_login(s.coach)
        assert reclaim(client, s, text="brace harder").status_code == 200

        # Simulate the #577 damage directly: a pre-fix `restore_plan_snapshot`
        # purge hard-deleted this row's line-0 cell out from under it, and
        # `LoggedSet.prescription` is SET_NULL on delete -- this is exactly
        # the state a buggy undo left behind, not a shortcut around
        # exercising it.
        LoggedSet.objects.filter(pk=original.pk).update(prescription=None)

        client.force_login(s.athlete)
        # The restore: identical text, back onto a line whose visible text is
        # now the coach's cue -- `mine` is empty, so the upsert reaches the
        # `existing` reuse lookup.
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        live = LoggedSet.objects.get(source_line=cell, prescription__isnull=False)
        assert live.pk != original.pk, (
            "the restore adopted the NULL-prescription row instead of "
            "minting a fresh, countable one"
        )
        assert live.prescription_id == s.squat.pk
        assert (live.load, live.reps) == ("225", "5")

        # This path is a blur (`athlete_cell_write`), not "Log session", so the
        # log stays PENDING -- `one_rm.derive_one_rm_values` is deliberately
        # DONE-only (see its docstring) and would read empty regardless of
        # the bug. `personal_records.personal_records`'s *live* read is the
        # one PENDING draft is supposed to reach.
        key = one_rm.key_str(s.squat.exercise_id, s.squat.name)
        assert key in personal_records.personal_records(s.athlete, unit=s.plan.unit), (
            "the restored set must count toward the athlete's records"
        )
