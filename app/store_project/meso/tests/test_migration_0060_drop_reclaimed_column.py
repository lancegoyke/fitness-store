"""``0060`` drops the dead ``meso_loggedset.reclaimed_line_id`` column (#578 4c).

``0059`` removed ``LoggedSet.reclaimed_line`` from the model state only, so
the column outlived the field. ``0060`` drops it from the database. Portable:
runs on SQLite (default) and Postgres (``TEST_DATABASE_URL``).

Rows are seeded with the current models: the model state at ``0059`` is the
current state (``0060`` changes the database only), and the leftover column is
set with raw SQL because the state no longer knows it.
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from store_project.meso.factories import SessionLogFactory
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_parse_at_commit import seed

TABLE = "meso_loggedset"
COLUMN = "reclaimed_line_id"
MESO_0059 = ("meso", "0059_loggedset_reclaimed_line_state_only")
MESO_0060 = ("meso", "0060_drop_loggedset_reclaimed_line_column")


def _columns():
    with connection.cursor() as cursor:
        return {
            col.name: col
            for col in connection.introspection.get_table_description(cursor, TABLE)
        }


def _indexes_on_column():
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, TABLE)
    return [
        name
        for name, info in constraints.items()
        if info["index"] and COLUMN in info["columns"]
    ]


def _row(pk):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {TABLE} WHERE id = %s", [pk])
        names = [d[0] for d in cursor.description]
        values = cursor.fetchone()
    return dict(zip(names, values, strict=True)) if values else None


@pytest.mark.django_db
def test_the_column_is_gone_after_migrate():
    assert COLUMN not in _columns()
    assert _indexes_on_column() == []


@pytest.mark.django_db(transaction=True)
def test_a_row_with_the_column_set_survives_with_its_fields_intact():
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes("meso")
    try:
        executor.migrate([MESO_0059])
        executor.loader.build_graph()
        assert COLUMN in _columns()

        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        # The database is at 0059 here, which predates #708's lift-stamp
        # columns, so the current model can't insert — use the 0059 model.
        historical = executor.loader.project_state([MESO_0059]).apps
        row = historical.get_model("meso", "LoggedSet").objects.create(
            session_log_id=log.pk,
            prescription_id=s.squat.pk,
            exercise_slot_id=s.squat.exercise_slot_id,
            set_number=1,
            reps="5",
            load="225",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET {COLUMN} = %s WHERE id = %s",
                [s.squat.pk, row.pk],
            )
        before = _row(row.pk)
        assert before[COLUMN] == s.squat.pk
        before.pop(COLUMN)

        executor = MigrationExecutor(connection)
        executor.migrate([MESO_0060])
        executor.loader.build_graph()

        after = _row(row.pk)
        assert after is not None
        assert COLUMN not in after
        assert after == before
        assert COLUMN not in _columns()
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_reverse_re_adds_a_nullable_indexed_column():
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes("meso")
    try:
        s = seed()
        log = SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        row = LoggedSet.objects.create(
            session_log=log, prescription=s.squat, set_number=1, reps="5", load="225"
        )

        executor.migrate([MESO_0059])
        executor.loader.build_graph()

        columns = _columns()
        assert COLUMN in columns
        assert columns[COLUMN].null_ok
        assert _row(row.pk)[COLUMN] is None
        assert _indexes_on_column() != []

        executor = MigrationExecutor(connection)
        executor.migrate([MESO_0060])
        executor.loader.build_graph()
        assert COLUMN not in _columns()
        assert _indexes_on_column() == []
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaf_nodes)
