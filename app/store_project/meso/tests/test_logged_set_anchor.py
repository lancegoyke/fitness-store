"""``LoggedSet.exercise_slot`` — the durable anchor (#578 C1).

``LoggedSet.prescription`` points at the line-0 ``Prescription`` *cell*, which
app code can hard-delete (the coach-undo stray-cell purge, #577) and which
Django admin can hard-delete with no guard at all (#581). The FK is
``SET_NULL``, so the row survives with ``prescription = NULL`` and, on
``main``, silently stops counting toward 1RM and PRs, because every
derivation filtered ``prescription__isnull=False``.

``exercise_slot`` is a second, independent FK straight to the durable
``ExerciseSlot`` — ORDINARY app code only ever *soft*-deletes that
(``deleted_at``); the designer's own delete/undo paths never hard-delete it,
so it survives exactly the hard deletes that null out ``prescription``. That
premise isn't absolute, though: a plan/mesocycle rebuild (``plan.mesocycles.
all().delete()`` in ``management/commands/meso_import_template.py`` and
``seed_meso_demo.py``) hard-deletes the whole tree, cascading ``Mesocycle``
→ ``SessionSlot`` → ``ExerciseSlot`` — see ``LoggedSet.exercise_slot``'s
model comment for why that path doesn't orphan anything either. This module
proves the two hard-delete paths (#577's guard regressing, #581's unguarded
admin) no longer silently detach a set, and that the model-level anchor
resolution (``anchor_slot``/``anchor_slot_id``) does the right thing in each
of its four states: ``exercise_slot`` set with a live ``prescription``,
``exercise_slot`` set with ``prescription`` NULL (the two hard-delete
scenarios below leave a row in this state), ``exercise_slot`` NULL with a
live ``prescription`` (the transitional mid-deploy case), and neither.

The first two tests (the two hard-delete scenarios) are RED against the
pre-C1 read path — verified by temporarily reverting the ``one_rm.py`` /
``personal_records.py`` consumer changes and confirming both fail for the
right reason (see the PR description / task report for the exact commands
run and output).
"""

import importlib
import json
from decimal import Decimal
from unittest import mock

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from store_project.meso import history
from store_project.meso import one_rm
from store_project.meso import personal_records
from store_project.meso import presenters
from store_project.meso import serializers
from store_project.meso import settle
from store_project.meso.admin import ExerciseSlotInline
from store_project.meso.admin import SessionSlotInline
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AthleteOneRm
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.models import SessionSlot
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_settle import quiet_since
from store_project.meso.tests.test_settle import set_activity
from store_project.meso.tests.test_undo_spares_logged_sets import _log_one_set
from store_project.meso.tests.test_undo_spares_logged_sets import (
    _seed_slot_without_cells,
)
from store_project.meso.tests.test_undo_spares_logged_sets import _undo
from store_project.meso.tests.test_undo_spares_logged_sets import _write_line
from store_project.users.factories import SuperAdminFactory
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


# -- 1. coach undo hard-deletes the line-0 cell (#577 guard regression) -----


class TestCoachUndoHardDeleteIsDefenseInDepth:
    """A coach undo that hard-deletes a cell must not detach the athlete's set.

    #582/#583/#584 already made the stray-cell purge SPARE a cell any
    ``LoggedSet`` points at (``history._cells_athlete_data_points_at``), so
    reaching the hard delete on today's code requires simulating a
    *regression* of that guard — monkeypatching it to report "nothing is
    athlete data". This is deliberate: the point of ``exercise_slot`` is
    defense in depth, so the derivation must not *depend* on that guard for
    correctness. ``test_undo_spares_logged_sets.py`` already covers the
    guard's own, ordinary behavior (the purge sparing the cell); this test
    covers what happens the day that guard has a bug.
    """

    def test_the_set_still_counts_after_the_cell_is_hard_deleted(
        self, client, monkeypatch
    ):
        coach, athlete, plan, week, session, slot = _seed_slot_without_cells()

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
        assert row.exercise_slot_id == slot.pk

        # Simulate the guard regression (see the class docstring) — NOT
        # today's real behavior, which already spares this cell.
        monkeypatch.setattr(history, "_cells_athlete_data_points_at", lambda pks: set())

        client.force_login(coach)
        assert _undo(client, plan).status_code == 200

        assert not Prescription.objects.filter(pk=cell.pk).exists(), (
            "the monkeypatched guard should have let the purge through"
        )
        row.refresh_from_db()
        assert row.prescription_id is None, "prescription should have gone NULL"
        assert row.exercise_slot_id == slot.pk, (
            "exercise_slot is a direct FK to the slot, untouched by deleting the cell"
        )

        key = one_rm.key_str(slot.exercise_id, slot.name)
        assert key in one_rm.derive_one_rm_values(athlete, unit=plan.unit), (
            "the set stopped counting toward the athlete's estimated 1RM"
        )
        assert key in personal_records.personal_records(athlete, unit=plan.unit), (
            "the set stopped counting toward the athlete's records"
        )
        new_records = personal_records.new_records_in(row.session_log)
        assert key in {r.key for r in new_records}, (
            "the set stopped being detectable as a new record"
        )


# -- 2. admin hard-deletes the line-0 cell, no guard at all (#581) ----------


class TestAdminDeleteIsDefenseInDepth:
    """Django admin has no guard against hard-deleting a live cell (#581).

    Unlike the coach-undo purge, there is no guard to regress here at all —
    ``PrescriptionAdmin`` is a plain ``ModelAdmin`` and a superuser can
    delete any row through the ordinary change-page confirmation. This is
    exactly the scenario ``exercise_slot`` exists to survive without anyone
    having to add admin-side protection. Closes #581.
    """

    def test_the_set_still_counts_after_an_admin_delete(self, client):
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        row = LoggedSetFactory(
            session_log=log,
            prescription=s.squat,
            set_number=1,
            reps="5",
            load="225",
            rpe="8",
        )
        assert row.exercise_slot_id == s.squat.exercise_slot_id
        key = one_rm.key_str(s.squat.exercise_id, s.squat.name)

        client.force_login(SuperAdminFactory())
        resp = client.post(
            reverse("admin:meso_prescription_delete", args=[s.squat.pk]),
            {"post": "yes"},
        )
        assert resp.status_code == 302, resp.content

        assert not Prescription.objects.filter(pk=s.squat.pk).exists()
        row.refresh_from_db()
        assert row.prescription_id is None
        assert row.exercise_slot_id == s.squat.exercise_slot_id

        assert key in one_rm.derive_one_rm_values(s.athlete, unit=s.plan.unit), (
            "the set stopped counting toward the athlete's estimated 1RM"
        )
        assert key in personal_records.personal_records(s.athlete, unit=s.plan.unit), (
            "the set stopped counting toward the athlete's records"
        )
        new_records = personal_records.new_records_in(row.session_log)
        assert key in {r.key for r in new_records}


# -- 3. the backfill migration -----------------------------------------------


MESO_0049 = ("meso", "0049_coachsubscription_cancel_at")
MESO_0050 = ("meso", "0050_loggedset_exercise_slot")
MESO_0051 = ("meso", "0051_backfill_loggedset_exercise_slot")


class TestBackfillMigration:
    """``0051`` fills ``exercise_slot`` from ``prescription``, reports the rest.

    Runs the real migration through ``MigrationExecutor`` against whatever
    backend the suite is pointed at (SQLite by default) — deliberately not
    skipped, unlike the Postgres-only ``0040`` repro this pattern is borrowed
    from: the backfill is a plain, portable ``Subquery`` update, not
    Postgres-specific behavior.
    """

    # `transaction=True` overrides the module-level plain `django_db` marker
    # for this one test: running actual schema migrations (0050's AddField,
    # reversed and re-applied) needs a real transaction, not the savepoint
    # `django_db` ordinarily wraps a test in — SQLite in particular refuses a
    # schema edit while FK checks are enabled mid-transaction (matches
    # `test_migration_0040_pending_triggers.py`'s own marker).
    @pytest.mark.django_db(transaction=True)
    def test_backfills_reports_leftovers_and_spares_already_anchored_rows(self, capsys):
        executor = MigrationExecutor(connection)
        leaf_nodes = executor.loader.graph.leaf_nodes("meso")

        try:
            # Roll `meso` back to just before the field exists at all — the
            # two baseline rows below are built with NO knowledge of
            # `exercise_slot`, exactly like every row `main` ever wrote.
            executor.migrate([MESO_0049])
            executor.loader.build_graph()

            # Everything except `LoggedSet` is untouched by 0050/0051, so the
            # ordinary factories/real models build it — only `LoggedSet`
            # itself needs the historical model matching each schema step.
            coach = UserFactory()
            athlete = UserFactory()
            rel = CoachAthleteFactory(coach=coach, athlete=athlete)
            plan = PlanFactory(relationship=rel)
            meso = MesocycleFactory(plan=plan, name="Block 1", order=0)
            week = WeekFactory(mesocycle=meso, index=1)
            session = day(week, day_number=1, name="Lower")
            slot_a = ExerciseSlot.objects.create(
                session_slot=session.session_slot, name="Squat", order=0
            )
            slot_b = ExerciseSlot.objects.create(
                session_slot=session.session_slot, name="Bench", order=1
            )
            prescription_a = Prescription.objects.create(
                exercise_slot=slot_a, week=week, line=0, text="5x5"
            )
            prescription_b = Prescription.objects.create(
                exercise_slot=slot_b, week=week, line=0, text="3x8"
            )
            log = SessionLog.objects.create(session=session, athlete=athlete)

            OldLoggedSet = executor.loader.project_state([MESO_0049]).apps.get_model(
                "meso", "LoggedSet"
            )
            with_prescription = OldLoggedSet.objects.create(
                session_log_id=log.pk,
                prescription_id=prescription_a.pk,
                set_number=1,
                reps="5",
                load="225",
            )
            without_prescription = OldLoggedSet.objects.create(
                session_log_id=log.pk,
                prescription_id=None,
                set_number=2,
                reps="5",
                load="135",
            )

            # One step forward: `0050` only ADDs the nullable column, so the
            # THIRD row — one that already carries an `exercise_slot`, built
            # to prove the backfill leaves an already-anchored row alone —
            # can only be constructed from here, before `0051` ever runs.
            executor.migrate([MESO_0050])
            executor.loader.build_graph()
            MidLoggedSet = executor.loader.project_state([MESO_0050]).apps.get_model(
                "meso", "LoggedSet"
            )
            # Deliberately anchored to `slot_a` even though its OWN
            # prescription is `prescription_b` (`slot_b`) — if the backfill
            # ignored its `exercise_slot__isnull=True` guard and overwrote
            # every row from `prescription`, this would end up on `slot_b`.
            already_anchored = MidLoggedSet.objects.create(
                session_log_id=log.pk,
                prescription_id=prescription_b.pk,
                exercise_slot_id=slot_a.pk,
                set_number=3,
                reps="8",
                load="95",
            )

            capsys.readouterr()  # discard anything captured so far
            executor.migrate([MESO_0051])
            captured = capsys.readouterr()

            # Re-fetched via the REAL, current model (0051 is the leaf this
            # branch's code matches) rather than `refresh_from_db()` on the
            # historical instances above — those are frozen at their own
            # migration state and have no `exercise_slot` attribute at all.
            with_prescription = LoggedSet.objects.get(pk=with_prescription.pk)
            without_prescription = LoggedSet.objects.get(pk=without_prescription.pk)
            already_anchored = LoggedSet.objects.get(pk=already_anchored.pk)

            assert with_prescription.exercise_slot_id == slot_a.pk, (
                "a row with a live prescription should be backfilled from it"
            )
            assert without_prescription.exercise_slot_id is None, (
                "a row with no prescription has nothing to backfill from"
            )
            assert already_anchored.exercise_slot_id == slot_a.pk, (
                "a row that already had an exercise_slot must not be disturbed"
            )
            assert str(without_prescription.pk) in captured.out, (
                "the leftover row's pk should be reported on stdout"
            )
        finally:
            executor.loader.build_graph()
            executor.migrate(leaf_nodes)


_backfill_0051 = importlib.import_module(
    "store_project.meso.migrations.0051_backfill_loggedset_exercise_slot"
)


class TestPartitionHelper:
    """``0051``'s ``_partition`` — the unrecoverable/raced split, with no database at all.

    The real "raced" branch (a ``LoggedSet`` whose ``prescription`` is live
    but which missed the migration's own ``UPDATE``) can't be constructed in
    a single-connection SQLite test — building one for real needs a second
    connection racing the migration's write lock, which
    ``TestBackfillMigration`` above can't do. Factoring the partition logic
    into a plain function of already-fetched ``(pk, prescription_id)`` pairs
    sidesteps that entirely: this pins both buckets — including one with a
    live ``prescription`` (raced) — without touching a database or a
    migration executor.
    """

    def test_splits_unrecoverable_from_raced(self):
        rows = [
            (1, None),  # unrecoverable: no prescription at all
            (2, 101),  # raced: prescription is live
            (3, None),  # unrecoverable
            (4, 102),  # raced
        ]

        unrecoverable_pks, raced_pks = _backfill_0051._partition(rows)

        assert unrecoverable_pks == [1, 3]
        assert raced_pks == [2, 4]

    def test_an_empty_read_partitions_to_two_empty_lists(self):
        assert _backfill_0051._partition([]) == ([], [])

    def test_all_unrecoverable_leaves_raced_empty(self):
        rows = [(1, None), (2, None)]

        unrecoverable_pks, raced_pks = _backfill_0051._partition(rows)

        assert unrecoverable_pks == [1, 2]
        assert raced_pks == []

    def test_all_raced_leaves_unrecoverable_empty(self):
        rows = [(1, 101), (2, 102)]

        unrecoverable_pks, raced_pks = _backfill_0051._partition(rows)

        assert unrecoverable_pks == []
        assert raced_pks == [1, 2]


# -- 4 & 5. the transitional anchor_slot fallback ----------------------------


class TestAnchorSlotResolution:
    """Two of the four ``anchor_slot``/``anchor_slot_id`` states, read straight off the model.

    This class pins the two states where ``exercise_slot`` is itself NULL:
    ``exercise_slot`` NULL with a live ``prescription`` (the transitional
    mid-deploy fallback) and neither pointer set (no identity at all). The
    other two — ``exercise_slot`` set with a live ``prescription``, and
    ``exercise_slot`` set with ``prescription`` NULL — are covered by the two
    hard-delete tests earlier in this module (``TestCoachUndoHardDeleteIs
    DefenseInDepth`` and ``TestAdminDeleteIsDefenseInDepth``): each asserts
    ``exercise_slot_id`` directly, both before its hard delete (the first
    state) and again after (the second) — and it's only the AFTER assertion
    that additionally proves that state resolves correctly, by driving
    ``anchor_slot_id``'s own consumers (``one_rm``/``personal_records``)
    rather than the property directly.
    """

    def test_exercise_slot_null_with_a_live_prescription_still_counts(self):
        """The transitional fallback: what an old container writes mid-deploy.

        Built by bypassing ``LoggedSet.save()`` — ``.filter(...).update(...)``
        rather than ``.create()`` — because item B's model-invariant
        ``save()`` override now fills ``exercise_slot`` from a live
        ``prescription`` on every ordinary write. A row with
        ``exercise_slot=None`` next to a live ``prescription`` can therefore
        no longer come from ``.create()`` (that would immediately backfill
        it via ``save()``); it can only come from code that predates the
        column — a stale container mid-deploy, or a bulk write that bypasses
        ``save()`` the way this construction deliberately does.
        """
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        row = LoggedSet.objects.create(
            session_log=log,
            prescription=s.squat,
            set_number=1,
            reps="5",
            load="225",
        )
        LoggedSet.objects.filter(pk=row.pk).update(exercise_slot=None)
        row.refresh_from_db()
        assert row.exercise_slot_id is None, "the update() bypass should have held"
        assert row.anchor_slot_id == s.squat.exercise_slot_id
        assert row.anchor_slot == s.squat.exercise_slot

        key = one_rm.key_str(s.squat.exercise_id, s.squat.name)
        assert key in one_rm.derive_one_rm_values(s.athlete, unit=s.plan.unit)
        assert key in personal_records.personal_records(s.athlete, unit=s.plan.unit)

    def test_a_row_with_neither_pointer_resolves_to_no_identity(self):
        """No crash, and excluded from every derivation — not counted as anything."""
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        orphan = LoggedSet.objects.create(
            session_log=log,
            prescription=None,
            exercise_slot=None,
            set_number=1,
            reps="5",
            load="225",
        )
        assert orphan.anchor_slot_id is None
        assert orphan.anchor_slot is None

        key = one_rm.key_str(s.squat.exercise_id, s.squat.name)
        assert key not in one_rm.derive_one_rm_values(s.athlete, unit=s.plan.unit)
        assert personal_records.personal_records(s.athlete, unit=s.plan.unit) == {}


# -- 6. presenters.session_results groups by the anchor slot, not the cell --


class TestSessionResultsSurvivesAHardDelete:
    """The coach's results grouping key is the anchor slot, not the cell pk.

    Pre-C1, ``session_results`` grouped logged sets by ``prescription_id`` —
    the pk of the SPECIFIC ``Prescription`` row a set was logged against. If
    that exact cell is hard-deleted (#577/#581) and a fresh line-0 cell is
    later created for the SAME ``ExerciseSlot`` (the coach re-adds the row,
    or a purge/undo cycle rebuilds it), the athlete's earlier set's
    ``prescription_id`` now points at nothing — under the old, cell-pk
    grouping it silently dropped out of that row's logged sets, showing "not
    yet logged" for a lift the athlete already did. Grouping by
    ``exercise_slot_id`` (#578 C1) survives the cell being replaced, because
    the new cell and the old set still share the same durable slot identity.
    """

    def test_the_row_still_shows_the_logged_sets_after_the_cell_is_replaced(self):
        s = seed()
        squat_slot = s.squat.exercise_slot
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        LoggedSetFactory(
            session_log=log,
            prescription=s.squat,
            set_number=1,
            reps="5",
            load="225",
            rpe="8",
        )

        # Hard-delete the cell the set was logged against, then rebuild a
        # fresh line-0 cell for the SAME slot/week — the cell instance
        # changes, the slot identity doesn't.
        Prescription.objects.filter(pk=s.squat.pk).delete()
        presc(
            exercise_slot=squat_slot,
            week=s.week,
            sets="3",
            reps="5",
            load="225",
            rpe="8",
        )

        result = presenters.session_results(s.session)

        assert result["summary"]["completion"] > 0, (
            "the logged set should still count toward completion"
        )
        rows_by_name = {row["name"]: row for row in result["rows"]}
        assert rows_by_name[squat_slot.name]["logged"] == "1×5 @ 225 kg", (
            "the row should still show what was logged, not an unlogged dash"
        )


# -- 7. settle.settle_log resolves the refreshed lift through the slot ------


class TestSettleResolvesThroughTheSlot:
    """Settling a quiet PENDING log must refresh 1RM off a NULL-prescription set.

    ``settle_log`` collects the lifts to refresh via each set's
    ``anchor_slot_id`` (#578 C1), not ``prescription_id`` — a set whose
    line-0 cell was hard-deleted (#577/#581) still resolves its lift through
    its own ``exercise_slot`` and must still refresh that lift's persisted
    ``AthleteOneRm`` when the log settles to DONE.
    """

    def test_settling_refreshes_the_one_rm_for_a_null_prescription_set(self):
        s = seed()
        key = one_rm.key_str(s.squat.exercise_id, s.squat.name)
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )
        LoggedSetFactory(
            session_log=log,
            prescription=s.squat,
            set_number=1,
            reps="5",
            load="225",
            rpe="8",
        )
        assert not AthleteOneRm.objects.filter(athlete=s.athlete, key=key).exists()

        Prescription.objects.filter(pk=s.squat.pk).delete()
        row = LoggedSet.objects.get(session_log=log)
        assert row.prescription_id is None, "the cell delete should SET_NULL it"
        assert row.exercise_slot_id == s.squat.exercise_slot_id

        set_activity(log, quiet_since())
        assert settle.settle_quiet_logs() == 1

        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE

        athlete_one_rm = AthleteOneRm.objects.get(athlete=s.athlete, key=key)
        assert athlete_one_rm.value == Decimal("262.5")  # Epley(225, 5)


# -- 8. serializers.last_logged_labels resolves the label through the slot --


class TestLastLoggedLabelsSurvivesAHardDelete:
    """The designer's "last time" label must survive a hard-deleted cell.

    ``last_logged_labels`` reads its ``LoggedSet`` queryset through
    ``.anchored()`` (#578 C1) rather than a bare ``prescription`` join, so a
    set whose line-0 cell was hard-deleted (#577/#581) but whose
    ``exercise_slot`` survives must still produce a "last time" label for
    that lift.
    """

    def test_the_lift_still_gets_a_last_time_label_after_the_hard_delete(self):
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        LoggedSetFactory(
            session_log=log,
            prescription=s.squat,
            set_number=1,
            reps="5",
            load="225",
            rpe="8",
        )

        Prescription.objects.filter(pk=s.squat.pk).delete()

        labels = serializers.last_logged_labels(s.plan, [s.squat], s.plan.unit)

        assert labels.get(s.squat.pk) == "1×5 · 225kg · RPE8", (
            "the lift should still show its last-logged summary"
        )


# -- 9. CASCADE blast radius + admin inlines can't reach it ------------------


class TestExerciseSlotCascadeBlastRadius:
    """Hard-deleting an ``ExerciseSlot`` deletes its ``LoggedSet`` rows.

    Nothing in this module pins what CASCADE (``LoggedSet.exercise_slot``'s
    model comment) actually does end to end — every other test here
    hard-deletes a ``Prescription`` (the line-0 cell), never the
    ``ExerciseSlot`` itself. Paired with the admin-inline guard: an inline
    delete calls ``obj.delete()`` straight from
    ``BaseModelFormSet.save_existing_objects()`` with no confirmation page,
    so ``ExerciseSlotInline``/``SessionSlotInline`` both set
    ``can_delete = False``.

    That guard is NOT "this CASCADE can only be triggered through a model's
    OWN admin page" — ``MesocycleInline`` (on ``PlanAdmin``) and
    ``WeekInline`` (on ``MesocycleAdmin``) have no ``can_delete = False`` and
    both reach ``LoggedSet`` too, via ``Mesocycle``/``Week`` → ``SessionSlot``/
    ``Session`` → ... The accurate boundary: no inline can silently destroy a
    ``LoggedSet`` row that ``origin/main`` would have preserved. A
    ``Mesocycle``/``Week`` inline delete also cascades ``Week`` → ``Session``
    → ``SessionLog`` → ``LoggedSet`` down the other branch, so those rows die
    on ``main`` too — not a divergence this FK introduces. Freezing
    ``MesocycleInline``/``WeekInline`` with ``can_delete = False`` would buy
    nothing for this PR, so they're deliberately left alone; only
    ``ExerciseSlotInline``/``SessionSlotInline`` — the two inlines whose
    silent delete would newly detach a ``LoggedSet`` that ``main`` would have
    kept alive as an orphan (``prescription = NULL``) — get the guard.
    """

    def test_hard_deleting_the_slot_cascades_to_the_logged_set(self):
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        row = LoggedSetFactory(
            session_log=log,
            prescription=s.squat,
            set_number=1,
            reps="5",
            load="225",
            rpe="8",
        )
        slot_id = s.squat.exercise_slot_id
        assert row.exercise_slot_id == slot_id

        ExerciseSlot.objects.get(pk=slot_id).delete()

        assert not LoggedSet.objects.filter(pk=row.pk).exists(), (
            "hard-deleting the ExerciseSlot should CASCADE to its LoggedSet rows"
        )
        assert not Prescription.objects.filter(pk=s.squat.pk).exists()

    def test_admin_inlines_refuse_the_delete(self, client):
        """Drives the real ``SessionSlotAdmin`` change form, not just the class attribute.

        Asserting ``can_delete is False`` alone would stay green even if a
        later ``get_formset``/``can_delete`` override re-enabled deletion at
        request time — the class attribute isn't what protects a live
        request. This POSTs the actual change-form payload, WITH an
        ``exercise_slots-0-DELETE=on`` field added by hand (the checkbox
        ``can_delete = False`` keeps out of the rendered page — this
        simulates a crafted POST, not a click nobody can make), and proves
        the ``ExerciseSlot`` and its ``LoggedSet`` still exist afterward:
        with no ``DELETE`` field declared on the form, Django's formset
        machinery has nothing to look at and silently ignores the extra key.
        """
        assert ExerciseSlotInline.can_delete is False
        assert SessionSlotInline.can_delete is False

        s = seed()
        squat_slot = s.squat.exercise_slot
        session_slot = squat_slot.session_slot
        slots = list(session_slot.exercise_slots.order_by("order"))
        assert slots[0].pk == squat_slot.pk

        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        row = LoggedSetFactory(
            session_log=log,
            prescription=s.squat,
            set_number=1,
            reps="5",
            load="225",
            rpe="8",
        )

        client.force_login(SuperAdminFactory())
        url = reverse("admin:meso_sessionslot_change", args=[session_slot.pk])
        data = {
            "mesocycle": str(session_slot.mesocycle_id),
            "name": session_slot.name,
            "order": str(session_slot.order),
            "bias": session_slot.bias,
            "day_number": str(session_slot.day_number),
            "deleted_at_0": "",
            "deleted_at_1": "",
            "exercise_slots-TOTAL_FORMS": str(len(slots)),
            "exercise_slots-INITIAL_FORMS": str(len(slots)),
            "exercise_slots-MIN_NUM_FORMS": "0",
            "exercise_slots-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }
        for i, slot in enumerate(slots):
            prefix = f"exercise_slots-{i}"
            data.update(
                {
                    f"{prefix}-id": str(slot.pk),
                    f"{prefix}-session_slot": str(slot.session_slot_id),
                    f"{prefix}-exercise": (
                        "" if slot.exercise_id is None else str(slot.exercise_id)
                    ),
                    f"{prefix}-name": slot.name,
                    f"{prefix}-order": str(slot.order),
                    f"{prefix}-tags": json.dumps(slot.tags),
                    f"{prefix}-tempo": slot.tempo,
                    f"{prefix}-rest": slot.rest,
                    f"{prefix}-note": slot.note,
                    f"{prefix}-deleted_at_0": "",
                    f"{prefix}-deleted_at_1": "",
                }
            )
        # The tampered field: never rendered (``can_delete = False``), added
        # here to prove the boundary holds even against a crafted POST, not
        # only against the real admin UI.
        data["exercise_slots-0-DELETE"] = "on"

        resp = client.post(url, data)
        if resp.status_code != 302:
            errors = [
                iaf.formset.errors for iaf in resp.context["inline_admin_formsets"]
            ]
            raise AssertionError(
                f"expected a successful save (302), got {resp.status_code}: "
                f"main form errors {resp.context['adminform'].form.errors!r}, "
                f"inline formset errors {errors!r}"
            )

        assert ExerciseSlot.objects.filter(pk=squat_slot.pk).exists(), (
            "a tampered DELETE field the inline never rendered should not "
            "delete the ExerciseSlot"
        )
        assert LoggedSet.objects.filter(pk=row.pk).exists(), (
            "the LoggedSet anchored to that slot should not have been "
            "cascaded away by a delete that never should have happened"
        )


# -- 10. the admin cannot create a NULL anchor (item B) ----------------------


class TestModelInvariantClosesTheAdminPath:
    """A ``LoggedSet`` saved with a ``prescription`` and no ``exercise_slot`` derives it.

    That's the ``save()`` invariant (review item B), and it's what makes it
    safe to leave ``exercise_slot`` ``readonly`` on ``LoggedSetInline`` (item
    C.2) rather than merely blocked-but-still-wrong.

    Most of this class pins that invariant directly against
    ``LoggedSet.save()`` — the behavior being proved lives entirely there, so
    a direct ``.save()`` assertion is the precise test.
    ``test_the_real_admin_inline_post_derives_the_anchor`` below also drives
    the actual ``LoggedSetInline`` POST end to end: hand-built
    management-form data DOES have a precedent in this suite —
    ``TestExerciseSlotCascadeBlastRadius.test_admin_inlines_refuse_the_delete``
    builds exactly that for ``ExerciseSlotInline`` — so there is no reason
    left to settle for the model-level test alone.
    """

    def test_a_plain_save_derives_the_anchor(self):
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        row = LoggedSet(
            session_log=log, prescription=s.squat, set_number=1, reps="5", load="225"
        )
        assert row.exercise_slot_id is None, "not derived yet"

        row.save()

        assert row.exercise_slot_id == s.squat.exercise_slot_id
        row.refresh_from_db()
        assert row.exercise_slot_id == s.squat.exercise_slot_id, (
            "the derived value must persist, not just live on the in-memory instance"
        )

    def test_an_update_fields_save_still_persists_the_derived_anchor(self):
        """The ``update_fields`` branch: a targeted save must not skip the fill."""
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        row = LoggedSet.objects.create(
            session_log=log, prescription=s.squat, set_number=1, reps="5", load="225"
        )
        LoggedSet.objects.filter(pk=row.pk).update(exercise_slot=None)
        row.refresh_from_db()
        assert row.exercise_slot_id is None

        row.reps = "6"
        row.save(update_fields=["reps"])

        row.refresh_from_db()
        assert row.reps == "6"
        assert row.exercise_slot_id == s.squat.exercise_slot_id, (
            "save(update_fields=[...]) must add 'exercise_slot' to the list "
            "passed to super().save(), or the derived fill never reaches the "
            "database even though the in-memory instance looks right"
        )

    def test_an_empty_update_fields_list_leaves_memory_and_db_agreeing(self):
        """``save(update_fields=[])`` must be a TRUE no-op, not just a DB no-op.

        Django's own ``Model.save()`` returns before writing anything when
        ``update_fields`` is an explicit, non-``None`` EMPTY list. Mutating
        ``self.exercise_slot_id`` before that guard would leave the in-memory
        instance re-anchored while the database keeps the old value — the
        opposite of a no-op, and a live divergence between what the caller's
        object says and what a fresh read would return.
        """
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        row = LoggedSet.objects.create(
            session_log=log, prescription=s.squat, set_number=1, reps="5", load="225"
        )
        LoggedSet.objects.filter(pk=row.pk).update(exercise_slot=None)
        row.refresh_from_db()
        assert row.exercise_slot_id is None

        row.save(update_fields=[])

        assert row.exercise_slot_id is None, (
            "an empty update_fields list must not re-anchor the in-memory "
            "instance either, not just skip writing it to the database"
        )
        row.refresh_from_db()
        assert row.exercise_slot_id is None, "and the database must be untouched"

    def test_a_re_pointed_prescription_re_derives_the_anchor(self):
        """An admin re-point of ``prescription`` re-files the set onto the new slot.

        RED against round 1's fill-WHEN-BLANK guard (``if self.exercise_slot_id
        is None and self.prescription_id is not None``): once ``exercise_slot_id``
        is set at all, that guard never fires again, so a ``LoggedSet`` whose
        ``prescription`` is later RE-POINTED at a different ``ExerciseSlot``'s
        cell — e.g. a staffer opening ``LoggedSetInline``, retyping the row's
        ``prescription`` raw-id from one slot to another, and Saving — would
        keep the OLD anchor forever, permanently disagreeing with its own
        ``prescription.exercise_slot``. The fix re-derives on every save with
        a live ``prescription``, not only when the anchor starts out blank.
        """
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        row = LoggedSet.objects.create(
            session_log=log, prescription=s.squat, set_number=1, reps="5", load="225"
        )
        assert row.exercise_slot_id == s.squat.exercise_slot_id, (
            "sanity: derived on create"
        )
        assert s.rdl.exercise_slot_id != s.squat.exercise_slot_id, (
            "sanity: the two cells must anchor different slots for this test to prove anything"
        )

        row.prescription = s.rdl
        row.save()

        assert row.exercise_slot_id == s.rdl.exercise_slot_id, (
            "re-pointing prescription at a different slot's cell should "
            "re-file the set's anchor onto that new slot, not leave the "
            "stale one behind"
        )
        row.refresh_from_db()
        assert row.exercise_slot_id == s.rdl.exercise_slot_id, (
            "the re-derived value must persist, not just live on the in-memory instance"
        )

    def test_the_real_admin_inline_post_derives_the_anchor(self, client):
        """Drives the real ``SessionLogAdmin``/``LoggedSetInline`` POST, not just ``.save()``.

        Reuses the hand-built management-form recipe
        ``TestExerciseSlotCascadeBlastRadius.test_admin_inlines_refuse_the_delete``
        established for ``ExerciseSlotInline``. ``exercise_slot`` is readonly
        on ``LoggedSetInline`` (never a form field the POST can name), so the
        only thing this proves that the ``.save()``-level tests above don't
        is that the REAL inline save path — ``BaseModelFormSet.
        save_existing_objects()`` → ``form.save()`` → ``LoggedSet.save()`` —
        still reaches the derivation with no ``exercise_slot`` key in the
        payload at all.

        Gotcha worth pinning in the comment, not just the code:
        ``save_existing_objects()`` only calls ``form.save()`` for a row
        whose form ``has_changed()`` — an unchanged existing form is skipped
        entirely, ``LoggedSet.save()`` included. So this POST must actually
        change some editable field (``reps``, here) for the derivation to
        fire at all; a POST that merely restates the row's current values
        would pass for the wrong reason (nothing ran), not the right one.
        """
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        row = LoggedSetFactory(
            session_log=log, prescription=s.squat, set_number=1, reps="5", load="225"
        )
        LoggedSet.objects.filter(pk=row.pk).update(exercise_slot=None)
        row.refresh_from_db()
        assert row.exercise_slot_id is None, "sanity: starts transitional"

        client.force_login(SuperAdminFactory())
        url = reverse("admin:meso_sessionlog_change", args=[log.pk])
        data = {
            "session": str(log.session_id),
            "athlete": str(log.athlete_id),
            "date": "",
            "status": log.status,
            "notes": "",
            "last_activity_at_0": "2026-01-01",
            "last_activity_at_1": "00:00:00",
            "sets-TOTAL_FORMS": "1",
            "sets-INITIAL_FORMS": "1",
            "sets-MIN_NUM_FORMS": "0",
            "sets-MAX_NUM_FORMS": "1000",
            "sets-0-id": str(row.pk),
            "sets-0-session_log": str(log.pk),
            "sets-0-prescription": str(s.squat.pk),
            "sets-0-set_number": "1",
            # Changed from the row's actual "5" — see the docstring: an
            # unchanged inline form is never saved at all.
            "sets-0-reps": "6",
            "sets-0-load": "225",
            "sets-0-rpe": "",
            "sets-0-source_line": "",
            "_save": "Save",
        }

        resp = client.post(url, data)
        if resp.status_code != 302:
            errors = [
                iaf.formset.errors for iaf in resp.context["inline_admin_formsets"]
            ]
            raise AssertionError(
                f"expected a successful save (302), got {resp.status_code}: "
                f"main form errors {resp.context['adminform'].form.errors!r}, "
                f"inline formset errors {errors!r}"
            )

        row.refresh_from_db()
        assert row.reps == "6", "sanity: the POST actually changed the row"
        assert row.exercise_slot_id == s.squat.exercise_slot_id, (
            "the real LoggedSetInline POST, with no exercise_slot field in "
            "the payload at all, must still derive the anchor from "
            "prescription via LoggedSet.save()"
        )


# -- 11. athlete_log_session: the anchor map survives a mid-request race -----


class TestAthleteLogSessionAnchorsThroughTheRace:
    """Item A: the anchor map must not depend on trainability.

    ``_clean_logged_sets`` validates the payload against
    ``session.trainable_cells()`` BEFORE ``athlete_log_session`` takes its
    lock; the view re-reads ``trainable_cells()`` a SECOND time inside the
    transaction, to build the ``exercise_slot`` map ``bulk_create`` uses. A
    coach's ``prescription_skip``/``prescription_delete`` committing in that
    gap can make a validated cell non-trainable by the second read.

    A single 200 POST to ``athlete_log_session`` actually calls
    ``trainable_cells()`` THREE times, not two: ``_clean_logged_sets``
    (validation, before the lock), the anchor-map build below (in-transaction,
    what this race targets), and ``refresh_one_rms``'s lift list (also
    in-transaction, after ``bulk_create``, unconditional on every save). This
    patches ``Session.trainable_cells`` to answer differently ONLY across the
    first two — full on the first (validation), missing the posted cell on
    the second (the anchor map) — to simulate that race directly, without
    needing a second real request to land mid-transaction. The patch is
    scoped to leave the THIRD call untouched (the real, unfiltered list),
    because stripping the posted cell from ``refresh_one_rms`` too would be a
    second, unrelated side effect this test isn't about and doesn't assert on.

    Deleting the fallback query item A adds to ``athlete_log_session`` (the
    ``Prescription.objects.filter(pk__in=missing)`` lookup) makes this fail:
    the posted set would persist with ``exercise_slot_id = None``.
    """

    def test_a_cell_that_stops_being_trainable_mid_request_still_anchors(self, client):
        s = seed()
        client.force_login(s.athlete)
        real_trainable_cells = Session.trainable_cells
        calls = {"n": 0}

        def flaky_trainable_cells(self):
            calls["n"] += 1
            cells = list(real_trainable_cells(self))
            if calls["n"] == 2:
                # The in-transaction anchor-map re-read (views.py ~1663), and
                # ONLY that one — the posted cell just stopped being
                # trainable (a skip/delete landed in the gap). The third call
                # (~2051, refresh_one_rms's lift list) is deliberately left
                # alone; see the class docstring.
                cells = [c for c in cells if c.pk != s.squat.pk]
            return cells

        with mock.patch.object(Session, "trainable_cells", flaky_trainable_cells):
            resp = client.post(
                reverse("meso:athlete_log_session", kwargs={"pk": s.session.pk}),
                data=json.dumps(
                    {
                        "sets": [
                            {
                                "prescription": s.squat.pk,
                                "set_number": 1,
                                "reps": "5",
                                "load": "225",
                            }
                        ]
                    }
                ),
                content_type="application/json",
            )

        assert resp.status_code == 200, resp.content
        assert calls["n"] == 3, (
            "the test must actually exercise all three reads — validation, "
            "the anchor map, and the 1RM refresh — or it isn't proving what "
            "the class docstring says it proves"
        )
        row = LoggedSet.objects.get(
            session_log__session=s.session, session_log__athlete=s.athlete
        )
        assert row.prescription_id == s.squat.pk
        assert row.exercise_slot_id == s.squat.exercise_slot_id, (
            "a cell that stopped being trainable mid-request must still get "
            "a real exercise_slot, not a NULL anchor"
        )

    def test_a_cell_whose_slot_moves_to_another_day_mid_request_still_anchors(
        self, client
    ):
        """RED against scoping the fallback query by day/week (round 2's mistake).

        A coach's ``prescription_move`` re-homes an ``ExerciseSlot`` onto a
        DIFFERENT day's ``SessionSlot`` with a plain
        ``ExerciseSlot.objects.filter(pk=...).update(session_slot_id=...)`` —
        no lock shared with this session's ``athlete_log_session`` request.
        If that commits in the same validated-but-not-yet-locked gap the
        other test above exercises, the in-transaction re-read of
        ``trainable_cells()`` naturally stops returning the posted cell too
        (``cells()`` joins ``exercise_slot__session_slot=self.session_slot``),
        landing it in ``missing`` — but this time the cell's row still
        physically exists, just filed under another day. A fallback query
        scoped to ``week=session.week, exercise_slot__session_slot=
        session.session_slot`` filters on the day the slot USED to be on and
        matches nothing, so it must fail this test (NULL anchor); the
        unscoped fallback still finds the cell by its own pk and passes.

        Unlike the previous test's synthetic per-call list filtering, the
        move here is a REAL, persisted ``.update()`` — fired once, between
        the first call (validation) and the second (the anchor map), mirroring
        exactly when ``prescription_move`` could actually land. It is not
        undone afterward, so the THIRD ``trainable_cells()`` call
        (``refresh_one_rms``'s lift list, ~2051) naturally reflects the same
        moved state too — an honest consequence of a real mutation, not a
        hidden side effect of this patch, and this test makes no assertion
        about that refresh either way.
        """
        s = seed()
        client.force_login(s.athlete)
        other_session_slot = SessionSlot.objects.create(
            mesocycle=s.week.mesocycle,
            day_number=99,
            name="Upper",
            bias="",
            order=99,
        )
        real_trainable_cells = Session.trainable_cells
        calls = {"n": 0}

        def flaky_trainable_cells(self):
            calls["n"] += 1
            if calls["n"] == 2:
                # The gap: a coach's prescription_move commits here, once,
                # re-homing the slot onto another day. No list-comprehension
                # filtering — the real query, re-run below (and by every
                # later call in this request), naturally excludes the cell
                # once its exercise_slot really points at another day's
                # SessionSlot.
                ExerciseSlot.objects.filter(pk=s.squat.exercise_slot_id).update(
                    session_slot_id=other_session_slot.pk
                )
            return list(real_trainable_cells(self))

        with mock.patch.object(Session, "trainable_cells", flaky_trainable_cells):
            resp = client.post(
                reverse("meso:athlete_log_session", kwargs={"pk": s.session.pk}),
                data=json.dumps(
                    {
                        "sets": [
                            {
                                "prescription": s.squat.pk,
                                "set_number": 1,
                                "reps": "5",
                                "load": "225",
                            }
                        ]
                    }
                ),
                content_type="application/json",
            )

        assert resp.status_code == 200, resp.content
        assert calls["n"] == 3, "the test must actually exercise all three reads"
        row = LoggedSet.objects.get(
            session_log__session=s.session, session_log__athlete=s.athlete
        )
        assert row.prescription_id == s.squat.pk
        assert row.exercise_slot_id == s.squat.exercise_slot_id, (
            "a cell whose slot moved to another day mid-request must still "
            "get its own real exercise_slot, not a NULL anchor — the day it "
            "currently sits on is irrelevant to which slot IS its anchor"
        )


# -- 12. the N+1 guard on the transitional fallback is pinned ----------------


class TestSessionResultsAnchorFallbackHasNoNPlus1:
    """``select_related("prescription")`` in ``session_results``'s ``Prefetch``.

    ``LoggedSetFactory`` always derives ``exercise_slot`` from its own
    ``prescription`` (see the factory), so no other test in this suite ever
    builds a set whose ``anchor_slot_id`` actually falls through to the
    ``.prescription`` hop — meaning ``test_results.py``'s
    ``django_assert_num_queries(11)`` never dereferences ``prescription`` at
    all, and would stay green even if ``select_related("prescription")`` were
    deleted from the ``Prefetch``. This test forces the transitional shape on
    purpose (bypassing ``save()`` the same way item B's test does), across
    MULTIPLE sets sharing one log, and pins the query count as fixed rather
    than growing with how many such sets exist — proof the join is doing
    real work: without it, each set's own, per-instance-uncached
    ``.prescription`` access would cost its own query.
    """

    def test_multiple_transitional_sets_cost_no_extra_queries(
        self, django_assert_num_queries
    ):
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        rows = [
            LoggedSetFactory(
                session_log=log,
                prescription=s.squat,
                set_number=n,
                reps="5",
                load="225",
                rpe="8",
            )
            for n in range(1, 4)
        ]
        LoggedSet.objects.filter(pk__in=[r.pk for r in rows]).update(exercise_slot=None)

        with django_assert_num_queries(11):
            presenters.session_results(s.session)
