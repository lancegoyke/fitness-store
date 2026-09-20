"""A coach redo/undo must never let a revived pk collide with a live coordinate (#583, #584).

``restore_plan_snapshot`` revives a snapshotted ``Prescription`` cell BY PK
(``history.py`` — a redo must put back the exact row a corresponding undo took
away, not a lookalike with a new pk). But ``Prescription`` also enforces
``unique_cell_slot_week_line`` on ``(exercise_slot, week, line)``, and that
COORDINATE — not the pk — is a cell's real identity; the pk is only ever an
implementation detail of reviving the same row. The verified reproduction on
#583:

1. Coach ``api_cell_line_write`` line 1 "brace hard" -> cell pk=3 at (slot,
   week, line 1).
2. Coach undo -> the purge (absent from the popped snapshot) hard-deletes
   pk=3; the redo stack now holds one entry whose snapshot has pk=3 at that
   coordinate.
3. Athlete blurs "225 x 5" onto the same sub-line -> ``athlete_cell_write``'s
   ``get_or_create`` mints a BRAND NEW pk=4 at the exact coordinate the purge
   just freed. This records no ``PlanAction`` (athlete writes never do, by
   design — a coach undo must never revert an athlete's own tracking), so
   nothing tells the still-pending redo its target coordinate is no longer
   vacant.
4. Coach redo -> tries to ``save()`` pk=3 straight onto a coordinate pk=4 now
   occupies -> ``IntegrityError`` on ``unique_cell_slot_week_line`` -> 500,
   whole redo rolled back.

The fix (``history.py``, the block right after ``existing_cells`` is built,
just above the cell upsert loop) resolves every ``(exercise_slot, week,
line)`` collision BEFORE that loop runs, applying the SAME rule the
stray-cell purge already states for a cell absent from the snapshot — just
one join earlier, to a coordinate a REVIVED pk wants rather than only to a
stray pk the purge would delete on its own:

* an occupant that is athlete data (``athlete_authored``, or a cell some
  ``LoggedSet`` still points at through ``prescription``/``source_line``/
  ``reclaimed_line`` — ``history._cells_athlete_data_points_at``) KEEPS the
  coordinate, and the snapshotted cell that wanted it is skipped entirely for
  this restore. This is the accepted cost, the same trade the ``logged_sets``
  spare clause already makes (#577): a coach's redo of that ONE line
  silently does nothing (the athlete's text and derived data are what
  survive) rather than 500ing the whole redo. A coach can retype a line;
  nobody can retype the athlete's performance.
* an occupant that is NOT athlete data (a coach-made row absent from the
  snapshot, sitting where a revived pk wants to go) is deleted — the exact
  same removal the stray-cell purge performs at the end of the function,
  just discovered here one step earlier.

ON THE "SWAP" EDGE CASE. If the collision is with ANOTHER snapshotted cell
(two rows trading coordinates within one restore), the fix deliberately does
NOT resolve it — ``unique_cell_slot_week_line`` is checked immediately, not
deferred, so neither pk can be written into the other's still-occupied
coordinate without first parking one of them somewhere temporary, and no
known real path produces this shape today. That case is left exactly as it
was before this fix (an ``IntegrityError`` out of the upsert loop); see the
comment in ``history.py`` at the point it's declined, and
``TestSnapshotSwapIsNotHandled`` below, which pins that this file does not
pretend otherwise.

ON REACHABILITY OF A COLLISION THROUGH THE **UNDO** PATH SPECIFICALLY (as
opposed to redo, which the reproduction above drives for real). Undo pops the
NEXT-older snapshot and restores it; for that restore to collide, the OLDER
snapshot would have to name a pk P at a coordinate some DIFFERENT pk P'
currently occupies. Every real pk churn at one coordinate happens through
EITHER this same purge (removing P, always as part of an undo/redo) or a
fresh coach write through ``cell_line_write`` (which reclaims an existing pk
rather than minting a new one, and — being a fresh mutation — clears the redo
stack via ``record_plan_action``, so it can never leave a stale entry for
some OTHER undo to collide against). The only writer that can leave a stale,
uncleared pk sitting at a freed coordinate is the athlete's
``athlete_cell_write`` — exactly the redo reproduction above. Reaching the
same shape via UNDO would need an even earlier purge to have already removed
P from that coordinate (i.e. a *second*, older undo/redo cycle stacked
underneath this one) — a real sequence the author did not find and does not
claim exists. Per the instructions for this file: state plainly, not imply.
**This is NOT verified as reachable through any real endpoint sequence** —
unlike ``test_undo_spares_logged_sets.py``'s own precondition (which the same
uncertainty applies to, for the same reason), the precondition here is stood
up directly by constructing a ``PlanAction`` UNDO row whose ``snapshot``
(plain JSON) names a stale pk. The fix in ``history.py`` does not
distinguish undo from redo at all — both call the same
``restore_plan_snapshot`` — so this test exists to pin that the SAME
collision-resolution code path is exercised (and does not regress) on the
undo leg too, not to claim the scenario arises unprompted in production.
"""

import pytest
from django.db.utils import IntegrityError
from django.urls import reverse

from store_project.meso import history
from store_project.meso.models import LoggedSet
from store_project.meso.models import PlanAction
from store_project.meso.models import Prescription
from store_project.meso.tests._helpers import presc
from store_project.meso.tests._helpers import sub_line
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell
from store_project.meso.tests.test_undo_spares_logged_sets import _undo
from store_project.meso.tests.test_undo_spares_logged_sets import _write_line

pytestmark = pytest.mark.django_db


def _redo(client, plan):
    return client.post(
        reverse("meso:api_plan_redo", kwargs={"plan_id": plan.pk}),
        content_type="application/json",
    )


class TestRedoAfterAnAthleteReclaimsTheFreedCoordinate:
    """The issue's exact sequence (#583), through the real endpoints."""

    def test_redo_survives_and_the_athletes_cell_and_set_are_intact(self, client):
        s = seed()

        client.force_login(s.coach)
        resp = _write_line(
            client, s.plan, s.squat.exercise_slot, s.week, line=1, text="brace hard"
        )
        assert resp.status_code == 200, resp.content
        coach_cell = sub_cell(s.squat, 1)
        coach_pk = coach_cell.pk

        assert _undo(client, s.plan).status_code == 200
        assert not Prescription.objects.filter(pk=coach_pk).exists(), (
            "setup assumption: the undo must have hard-deleted the coach's cell "
            "via the stray-cell purge, freeing the coordinate"
        )

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200, resp.content
        athlete_cell = sub_cell(s.squat, 1)
        assert athlete_cell.pk != coach_pk, (
            "setup assumption: athlete_cell_write must have minted a NEW pk at "
            "the freed coordinate, not reused the coach's old one"
        )
        assert athlete_cell.athlete_authored is True
        logged = LoggedSet.objects.get(source_line=athlete_cell)
        assert logged.prescription_id == s.squat.pk

        client.force_login(s.coach)
        resp = _redo(client, s.plan)
        assert resp.status_code == 200, (
            f"redo 500ed instead of resolving the coordinate collision: {resp.content}"
        )

        # Assert on the final cell rows the way the issue does: the redo must
        # not have touched the athlete's line, and must not have resurrected
        # the coach's old pk (or any second row) at that coordinate.
        rows = sorted(
            Prescription.objects.filter(
                exercise_slot=s.squat.exercise_slot, week=s.week
            ).values_list("pk", "exercise_slot_id", "week_id", "line", "text"),
            key=lambda row: row[3],
        )
        assert rows == [
            (s.squat.pk, s.squat.exercise_slot_id, s.week.pk, 0, s.squat.text),
            (athlete_cell.pk, s.squat.exercise_slot_id, s.week.pk, 1, "225 x 5"),
        ]

        # The accepted cost, stated so it can't drift: the coach's redone
        # "brace hard" is gone for good — the athlete's coordinate wins, and
        # this redo is a no-op for that one line. Same trade #577 already
        # makes for the `logged_sets` spare clause.
        athlete_cell.refresh_from_db()
        assert athlete_cell.text == "225 x 5"
        assert athlete_cell.athlete_authored is True

        logged.refresh_from_db()
        assert logged.source_line_id == athlete_cell.pk, (
            "the athlete's derived set lost its link to the sub-line it was parsed from"
        )
        assert logged.prescription_id == s.squat.pk, (
            "the athlete's derived set stopped counting toward the exercise's line-0 cell"
        )


class TestUndoWithAStaleSnapshotCollidingWithAnAthleteCell:
    """The undo leg of the same collision-resolution code path.

    See the module docstring's "ON REACHABILITY" section: this precondition
    (an UNDO ``PlanAction`` whose snapshot names a pk at a coordinate an
    athlete cell now holds) is **not verified reachable through any real
    endpoint sequence** — the author looked and did not find one, and says so
    rather than implying otherwise. It is stood up directly, the same way
    ``test_undo_spares_logged_sets.py`` stands up its own precondition: a
    ``PlanAction.snapshot`` is plain JSON, and constructing one by hand is a
    legitimate way to pin behavior on a code path shared with a case
    (redo, above) that IS verified reachable.
    """

    def test_undo_survives_and_the_athletes_cell_and_set_are_intact(self, client):
        s = seed()
        coach_cell = sub_line(s.squat, "old coach note", line=1)
        coach_pk = coach_cell.pk
        stale_snapshot = history.serialize_plan_snapshot(s.plan)

        # Stand in for "an earlier undo/redo cycle already purged this row" —
        # see the module docstring for why no single real request reaches
        # this precondition directly.
        Prescription.objects.filter(pk=coach_pk).delete()

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200, resp.content
        athlete_cell = sub_cell(s.squat, 1)
        assert athlete_cell.pk != coach_pk
        logged = LoggedSet.objects.get(source_line=athlete_cell)

        PlanAction.objects.create(
            plan=s.plan,
            stack=PlanAction.Stack.UNDO,
            seq=1,
            label="Edited Box Squat",
            snapshot=stale_snapshot,
        )

        client.force_login(s.coach)
        assert not Prescription.objects.filter(pk=coach_pk).exists()
        resp = client.post(
            reverse("meso:api_plan_undo", kwargs={"plan_id": s.plan.pk}),
            content_type="application/json",
        )
        assert resp.status_code == 200, (
            f"undo 500ed instead of resolving the coordinate collision: {resp.content}"
        )

        athlete_cell.refresh_from_db()
        assert athlete_cell.text == "225 x 5"
        assert athlete_cell.athlete_authored is True
        logged.refresh_from_db()
        assert logged.source_line_id == athlete_cell.pk
        assert logged.prescription_id == s.squat.pk


class TestStrayWithNoAthleteDataIsPurgedAndTheSnapshottedPkRevives:
    """The OTHER branch: an occupant that is NOT athlete data must still go.

    Sparing every collision would silently turn the collision guard into a
    second, earlier stray-cell purge that never actually purges — this test
    is what keeps that from happening unnoticed.
    """

    def test_stray_is_deleted_and_the_snapshotted_pk_is_revived_with_its_text(
        self, client
    ):
        s = seed()
        coach_cell = sub_line(s.squat, "old coach note", line=1)
        original_pk = coach_cell.pk
        snapshot = history.serialize_plan_snapshot(s.plan)

        # Stand in for "an earlier undo already purged it" (see the other two
        # test classes' docstrings for why a bare ORM delete, not a second
        # real undo, is used here).
        Prescription.objects.filter(pk=original_pk).delete()

        # A coach-made row lands at the SAME coordinate afterward — created
        # directly, not through an endpoint, precisely so it carries NO
        # athlete_authored flag and NO LoggedSet of any kind: the plainest
        # possible stray.
        stray = Prescription.objects.create(
            exercise_slot=s.squat.exercise_slot,
            week=s.week,
            line=1,
            text="new stray text",
        )
        assert stray.pk != original_pk

        history.restore_plan_snapshot(s.plan, snapshot)

        assert not Prescription.objects.filter(pk=stray.pk).exists(), (
            "a coach-made stray occupying a snapshotted coordinate must still be purged"
        )
        revived = Prescription.objects.get(pk=original_pk)
        assert revived.exercise_slot_id == s.squat.exercise_slot_id
        assert revived.week_id == s.week.pk
        assert revived.line == 1
        assert revived.text == "old coach note"
        assert revived.athlete_authored is False


class TestSnapshotSwapIsNotHandled:
    """Pins that a genuine within-snapshot coordinate SWAP is left as-is.

    Not a regression test for a fix — the opposite: a documentation-by-test
    that this narrower case was consciously left alone (see the module
    docstring's "ON THE SWAP EDGE CASE" and the matching comment in
    ``history.py``), so a future change either fixes it on purpose or this
    test is the thing that has to be updated to say so, rather than the gap
    reappearing silently.
    """

    def test_two_snapshotted_cells_trading_coordinates_still_raises(self, client):
        s = seed()
        cell_a = sub_line(s.squat, "line one", line=1)
        cell_b = sub_line(s.squat, "line two", line=2)
        snapshot = history.serialize_plan_snapshot(s.plan)

        # Swap what's live at each coordinate — the snapshot above still
        # names cell_a at line 1 / cell_b at line 2, but the DB now disagrees.
        # A direct two-step update would itself collide on the unique
        # constraint mid-swap (cell_a -> line 2 while cell_b still sits
        # there), so this parks cell_a on an unused line first, exactly the
        # "move to a temporary, unclaimed coordinate" maneuver the module
        # docstring says the real fix would need and does not attempt.
        Prescription.objects.filter(pk=cell_a.pk).update(line=99)
        Prescription.objects.filter(pk=cell_b.pk).update(line=1)
        Prescription.objects.filter(pk=cell_a.pk).update(line=2)

        with pytest.raises(IntegrityError):
            history.restore_plan_snapshot(s.plan, snapshot)


class TestCollisionGuardSparesAnOccupantUnderASoftDeletedCoordinate:
    """A collision occupant the purge itself would never touch must not be purged either.

    The stray-cell purge at the end of ``restore_plan_snapshot`` only ever
    hard-deletes a cell whose ``ExerciseSlot`` **and** ``Week`` are BOTH live
    in the snapshot being restored (``live_exercise_slot_pks_in_snapshot`` /
    ``live_week_pks_in_snapshot``) — a cell sitting under a slot or week the
    snapshot itself records as soft-deleted is deliberately left alone,
    because it's already hidden via that join without touching the row. The
    coordinate-collision guard above it (#583, #584) applies the SAME rule
    "one join earlier", in the ``coord[0] not in
    live_exercise_slot_pks_in_snapshot or coord[1] not in
    live_week_pks_in_snapshot`` branch right before
    ``stray_candidate_pks.add(occupant_pk)`` in ``history.py``: an occupant
    the guard would otherwise delete to make room for a revived pk is instead
    spared when the purge downstream of it would spare it too, and the
    snapshotted cell that wanted the coordinate is skipped rather than
    revived onto it. This test pins exactly that: the occupant it builds
    (``Q``) survives, and the stale snapshotted pk (``P``) — whose coordinate
    sits under a slot the snapshot itself records as soft-deleted — is not
    revived over it. Before that scoping existed, this same setup purged
    ``Q`` and revived ``P`` in its place, exactly backwards from what's
    asserted below; the rule this test pins is "the collision guard removes
    an occupant only when the purge itself would."

    ON REACHABILITY. This shape is **not known to be reachable through any
    real endpoint sequence** — same honesty as this file's other stood-up
    preconditions (``TestUndoWithAStaleSnapshotCollidingWithAnAthleteCell``,
    above). Getting a snapshot to name a stale pk P at a coordinate under a
    slot it also records as soft-deleted is plausible on its own (a coach
    deletes a row via a real endpoint, which soft-deletes its
    ``ExerciseSlot`` in place without touching any of that row's cells, so
    the very next ``PlanAction`` snapshot naturally captures both facts
    together). What is NOT verified is a real path that then makes P vanish
    from the database entirely while that particular snapshot is still the
    one a later undo/redo restores — the purge itself would never do it
    (P's slot isn't live, so the purge — before OR after this fix — always
    skips it), so it would take some OTHER, older undo/redo cycle purging P
    under a DIFFERENT, earlier snapshot where its slot was still live, timed
    so this snapshot is still on the stack afterward. The author did not
    find or attempt that sequence. Both P's disappearance and Q's arrival at
    the freed coordinate are stood up directly here, the same way this
    file's other hand-built-snapshot tests stand up their own preconditions.
    """

    def test_the_occupant_survives_and_the_stale_pk_is_not_revived(self, client):
        s = seed()

        # A row isolated from `s.squat`/`s.rdl` so soft-deleting its
        # `ExerciseSlot` below touches nothing else in the plan.
        row = presc(s.session, name="Isolation Row", sets="3", reps="10")
        dead_slot = row.exercise_slot
        stale_cell = sub_line(row, "old coach note", line=1)
        stale_pk = stale_cell.pk

        # Soft-delete the ROW itself — exactly what a real "delete this
        # exercise" endpoint does (`ExerciseSlot.soft_delete()`), and exactly
        # the condition the purge's own `live_exercise_slot_pks_in_snapshot`
        # rule cares about. Doing this BEFORE taking the snapshot means the
        # snapshot below both records `dead_slot` as soft-deleted AND still
        # names `stale_cell` at its coordinate — a cell carries no
        # `deleted_at` of its own, so `serialize_plan_snapshot` captures it
        # regardless of whether its slot is live.
        dead_slot.soft_delete()
        snapshot = history.serialize_plan_snapshot(s.plan)
        dead_slot_row = next(
            r for r in snapshot["exercise_slots"] if r["pk"] == dead_slot.pk
        )
        assert dead_slot_row["deleted_at"] is not None, (
            "setup assumption: the snapshot must record the row's ExerciseSlot "
            "as soft-deleted"
        )
        assert any(r["pk"] == stale_pk for r in snapshot["cells"]), (
            "setup assumption: the snapshot must still name the stale cell "
            "at its coordinate, dead slot notwithstanding"
        )

        # Stand in for "P no longer exists" — see the class docstring's ON
        # REACHABILITY section for why a bare ORM delete, not a second real
        # undo/redo cycle, is used here, matching every other hand-built
        # precondition in this file.
        Prescription.objects.filter(pk=stale_pk).delete()

        # A DIFFERENT, non-athlete-data cell lands at the exact same
        # coordinate afterward — created directly, not through an endpoint,
        # so it carries NO athlete_authored flag and NO LoggedSet of any
        # kind: the plainest possible occupant, same idiom as the stray in
        # `TestStrayWithNoAthleteDataIsPurgedAndTheSnapshottedPkRevives`.
        occupant = Prescription.objects.create(
            exercise_slot=dead_slot, week=s.week, line=1, text="new occupant text"
        )
        assert occupant.pk != stale_pk

        history.restore_plan_snapshot(s.plan, snapshot)

        occupant.refresh_from_db()
        assert occupant.text == "new occupant text", (
            "an occupant sitting under a slot the snapshot itself records as "
            "soft-deleted must be spared — the purge downstream of this guard "
            "would never touch it either, and the collision guard must not be "
            "stricter than the purge it's mirroring"
        )
        assert not Prescription.objects.filter(pk=stale_pk).exists(), (
            "a stale snapshotted cell whose slot is soft-deleted in the "
            "snapshot must not be revived onto a coordinate a spared occupant "
            "already holds"
        )
