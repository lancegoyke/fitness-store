"""``0063`` backfills ``LoggedSet.exercise`` / ``exercise_name`` (#708).

``0062`` added the two nullable stamp columns; ``0063`` stamps the rows written
before them from the slot they resolve to today (``exercise_slot``, else the
slot of ``prescription``) and leaves every other row alone. Portable: runs on
SQLite (default) and Postgres (``TEST_DATABASE_URL``).

The ``LoggedSet`` rows are inserted and read through the ``0062`` historical
model, not the current one, so a later migration that adds a column to the
table can't break this test (as #708's own columns broke the 0060 test's
current-model insert). The surrounding plan is built with the factories.
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from store_project.exercises.factories import ExerciseFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.models import LoggedSet
from store_project.meso.tests.test_parse_at_commit import seed

MESO_0062 = ("meso", "0062_loggedset_lift_stamp_708")
MESO_0063 = ("meso", "0063_backfill_loggedset_lift_stamp_708")


def _stamp(pk):
    return (
        LoggedSet.objects.filter(pk=pk)
        .values_list("exercise_id", "exercise_name")
        .get()
    )


@pytest.mark.django_db(transaction=True)
def test_backfill_stamps_unstamped_rows_and_leaves_stamped_ones():
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes("meso")
    try:
        executor.migrate([MESO_0062])
        executor.loader.build_graph()

        s = seed()
        ex = ExerciseFactory(name="Box Squat", slug="box-squat")
        slot = s.squat.exercise_slot
        slot.exercise = ex
        slot.save(update_fields=["exercise"])
        log = SessionLogFactory(session=s.session, athlete=s.athlete)

        HistoricalLoggedSet = executor.loader.project_state([MESO_0062]).apps.get_model(
            "meso", "LoggedSet"
        )

        def make(n):
            # The historical model has no ``save()`` override, so the stamp
            # starts NULL, the way old code left it.
            return HistoricalLoggedSet.objects.create(
                session_log_id=log.pk,
                prescription_id=s.squat.pk,
                exercise_slot_id=slot.pk,
                set_number=n,
                reps="5",
                load="100",
            ).pk

        anchored = make(1)  # (a) exercise_slot set, stamp NULL
        via_cell = make(2)  # (b) exercise_slot NULL, prescription set
        stamped = make(3)  # (c) already stamped with a different lift
        orphan = make(4)  # (d) nothing to resolve from
        HistoricalLoggedSet.objects.filter(pk=via_cell).update(exercise_slot=None)
        HistoricalLoggedSet.objects.filter(pk=orphan).update(
            exercise_slot=None, prescription=None
        )
        HistoricalLoggedSet.objects.filter(pk=stamped).update(
            exercise=None, exercise_name="Deadlift"
        )
        assert _stamp(anchored) == (None, None)
        assert _stamp(via_cell) == (None, None)

        executor = MigrationExecutor(connection)
        executor.migrate([MESO_0063])
        executor.loader.build_graph()

        assert _stamp(anchored) == (ex.pk, "Box Squat")
        assert _stamp(via_cell) == (ex.pk, "Box Squat")
        assert _stamp(stamped) == (None, "Deadlift")
        assert _stamp(orphan) == (None, None)
        # The backfill only fills the stamp; the anchors are as they were.
        assert (
            LoggedSet.objects.filter(pk=via_cell)
            .values_list("exercise_slot_id", flat=True)
            .get()
            is None
        )

        # Idempotent: reverse is a no-op and a second pass changes nothing.
        executor = MigrationExecutor(connection)
        executor.migrate([MESO_0062])
        executor.loader.build_graph()
        assert _stamp(anchored) == (ex.pk, "Box Squat")
        slot.name = "Renamed Since"
        slot.save(update_fields=["name"])
        executor = MigrationExecutor(connection)
        executor.migrate([MESO_0063])
        executor.loader.build_graph()
        assert _stamp(anchored) == (ex.pk, "Box Squat")
        assert _stamp(stamped) == (None, "Deadlift")
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaf_nodes)
