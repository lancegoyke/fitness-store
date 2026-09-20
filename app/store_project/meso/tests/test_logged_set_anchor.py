"""``LoggedSet.exercise_slot`` — the durable anchor (#578 C1).

``LoggedSet.prescription`` points at the line-0 ``Prescription`` *cell*, which
app code can hard-delete (the coach-undo stray-cell purge, #577) and which
Django admin can hard-delete with no guard at all (#581). The FK is
``SET_NULL``, so the row survives with ``prescription = NULL`` and, on
``main``, silently stops counting toward 1RM and PRs, because every
derivation filtered ``prescription__isnull=False``.

``exercise_slot`` is a second, independent FK straight to the durable
``ExerciseSlot`` — app code only ever *soft*-deletes that (``deleted_at``),
never hard-deletes it — so it survives exactly the hard deletes that null out
``prescription``. This module proves the two hard-delete paths (#577's guard
regressing, #581's unguarded admin) no longer silently detach a set, and that
the model-level anchor resolution (``anchor_slot``/``anchor_slot_id``) does
the right thing in each of its four states: exercise_slot set, exercise_slot
NULL with a live prescription (the transitional mid-deploy case), and neither.

The first two tests (the two hard-delete scenarios) are RED against the
pre-C1 read path — verified by temporarily reverting the ``one_rm.py`` /
``personal_records.py`` consumer changes and confirming both fail for the
right reason (see the PR description / task report for the exact commands
run and output).
"""

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


# -- 4 & 5. the transitional anchor_slot fallback ----------------------------


class TestAnchorSlotResolution:
    """``anchor_slot``/``anchor_slot_id``'s four states, read straight off the model."""

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
    ``can_delete = False`` (review item C.1) precisely so this CASCADE, real
    as it is, can only be triggered through a model's OWN admin page — never
    silently through an inline Save.
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

    def test_admin_inlines_refuse_the_delete(self):
        assert ExerciseSlotInline.can_delete is False
        assert SessionSlotInline.can_delete is False


# -- 10. the admin cannot create a NULL anchor (item B) ----------------------


class TestModelInvariantClosesTheAdminPath:
    """A ``LoggedSet`` saved with a ``prescription`` and no ``exercise_slot`` derives it.

    That's the ``save()`` invariant (review item B), and it's what makes it
    safe to leave ``exercise_slot`` ``readonly`` on ``LoggedSetInline`` (item
    C.2) rather than merely blocked-but-still-wrong.

    Driving the real admin ``LoggedSetInline`` POST end to end would need
    hand-built management-form data with no precedent anywhere in this
    suite; the behavior being pinned lives entirely in ``LoggedSet.save()``,
    so a direct ``.save()`` assertion is the honest test here, per the
    review's own "acceptable if driving the inline formset is
    disproportionate."
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


# -- 11. athlete_log_session: the anchor map survives a mid-request race -----


class TestAthleteLogSessionAnchorsThroughTheRace:
    """Item A: the anchor map must not depend on trainability.

    ``_clean_logged_sets`` validates the payload against
    ``session.trainable_cells()`` BEFORE ``athlete_log_session`` takes its
    lock; the view re-reads ``trainable_cells()`` a SECOND time inside the
    transaction, to build the ``exercise_slot`` map ``bulk_create`` uses. A
    coach's ``prescription_skip``/``prescription_delete`` committing in that
    gap can make a validated cell non-trainable by the second read. This
    patches ``Session.trainable_cells`` to answer differently across its two
    calls within one request — full on the first (validation), missing the
    posted cell on the second (in-transaction) — to simulate that race
    directly, without needing a second real request to land mid-transaction.

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
            if calls["n"] > 1:
                # The in-transaction re-read: the posted cell just stopped
                # being trainable (a skip/delete landed in the gap).
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
        assert calls["n"] >= 2, "the test must actually exercise both reads"
        row = LoggedSet.objects.get(
            session_log__session=s.session, session_log__athlete=s.athlete
        )
        assert row.prescription_id == s.squat.pk
        assert row.exercise_slot_id == s.squat.exercise_slot_id, (
            "a cell that stopped being trainable mid-request must still get "
            "a real exercise_slot, not a NULL anchor"
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
