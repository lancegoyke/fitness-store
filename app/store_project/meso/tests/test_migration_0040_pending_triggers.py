"""Reproduction test for the ``0040_remove_group_subsystem`` Postgres failure.

``0040``'s first operation is a ``RunPython`` that ORM-cascade-deletes every
group-rooted ``Plan`` (``relationship`` NULL). On PostgreSQL, Django's FK
constraints in this app are ``DEFERRABLE INITIALLY DEFERRED``, so that delete
queues deferred RI trigger events. The migration then runs
``RemoveConstraint``/``RemoveField``/``AlterField`` against ``meso_plan`` in
the SAME transaction, and Postgres refuses to ``ALTER TABLE`` a table with
pending trigger events. On an EMPTY database the delete matches zero rows and
queues nothing, so the migration passes there — which is why CI (empty test
DB) and prod (no group-rooted plans) were both green while a database that
actually had group-rooted plans (a restore, or a local/staging DB with real
data) would fail. This module reproduces that failure by building a
group-rooted plan tree at the ``0039`` migration state and then migrating
forward through ``0040``.

This does NOT reproduce on SQLite (no deferred-constraint concept there), so
it is skipped unless the configured DB backend is PostgreSQL. The default
test settings (``config.settings.test``) use an in-memory SQLite database, so
this test SKIPS under ``uv run pytest`` / ``just test`` as normally run. To
actually exercise it, point the suite at a Postgres backend, e.g.:

    uv run pytest --ds=config.settings.local app/store_project/meso/tests/test_migration_0040_pending_triggers.py

(``config.settings.local`` uses the same Postgres the dev DB does, via the
``SQL_*`` env vars in ``.env`` — see ``just services``.)
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from store_project.users.models import User

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason=(
            "The 'pending trigger events' failure is a PostgreSQL-only "
            "deferred-constraint behavior; it cannot reproduce on SQLite."
        ),
    ),
]

MESO_0039 = ("meso", "0039_drop_structured_cell_columns")
MESO_0040 = ("meso", "0040_remove_group_subsystem")


def test_0040_migrates_forward_with_group_rooted_plan_data():
    """0040 must not blow up on a DB that actually has group-rooted plans.

    Builds a full group-rooted ``Plan`` -> ``Mesocycle`` -> (``Week``,
    ``SessionSlot``) -> (``Session``, ``ExerciseSlot``) -> ``Prescription``
    tree at the ``0039`` schema state (mirroring ``Plan.scaffold``'s shape),
    so ``delete_shared_group_plans`` has real rows to cascade-delete and
    genuinely queues deferred trigger events on ``meso_plan``'s referencing
    tables. Then migrates forward through ``0040`` and asserts it completes.
    """
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes("meso")

    try:
        executor.migrate([MESO_0039])
        executor.loader.build_graph()

        old_apps = executor.loader.project_state([MESO_0039]).apps
        MesoGroup = old_apps.get_model("meso", "MesoGroup")
        Plan = old_apps.get_model("meso", "Plan")
        Mesocycle = old_apps.get_model("meso", "Mesocycle")
        Week = old_apps.get_model("meso", "Week")
        SessionSlot = old_apps.get_model("meso", "SessionSlot")
        ExerciseSlot = old_apps.get_model("meso", "ExerciseSlot")
        Session = old_apps.get_model("meso", "Session")
        Prescription = old_apps.get_model("meso", "Prescription")

        # The real, current User model — not the historical one from
        # ``old_apps``. ``users`` isn't the app under test, its dependency
        # snapshot at 0039 can be older than the actual (fully-migrated)
        # ``users_user`` table, missing defaults added by later users
        # migrations (e.g. ``points``). Assigning it to the historical
        # ``MesoGroup.coach`` FK via ``coach_id=`` sidesteps the ORM's
        # isinstance check (which would reject a cross-registry model).
        coach = User.objects.create(
            username="coach-0040-repro", email="coach-0040-repro@example.com"
        )
        group = MesoGroup.objects.create(name="Repro Group", coach_id=coach.pk)
        # relationship=None + group=<MesoGroup> is exactly what
        # ``delete_shared_group_plans`` targets (``relationship__isnull=True``)
        # and satisfies the ``plan_relationship_xor_group`` check constraint.
        plan = Plan.objects.create(title="Group plan", group=group)
        mesocycle = Mesocycle.objects.create(
            plan=plan, name="Block 1", order=0, week_count=4
        )
        week = Week.objects.create(
            mesocycle=mesocycle, index=1, phase="Accum", volume=70, intensity=65
        )
        slot = SessionSlot.objects.create(
            mesocycle=mesocycle, day_number=1, name="Day 1", order=0
        )
        Session.objects.create(week=week, session_slot=slot)
        exercise_slot = ExerciseSlot.objects.create(
            session_slot=slot, name="Squat", order=0
        )
        Prescription.objects.create(exercise_slot=exercise_slot, week=week)

        # Sanity: the row 0040's data step targets actually exists, and the
        # rest of the tree hangs off it so the cascade has real work to do.
        assert Plan.objects.filter(relationship__isnull=True).count() == 1
        assert Mesocycle.objects.filter(plan=plan).count() == 1
        assert Prescription.objects.filter(exercise_slot=exercise_slot).count() == 1

        # The assertion under test: this must not raise
        # "cannot ALTER TABLE ... because it has pending trigger events".
        executor.migrate([MESO_0040])
    finally:
        # Always restore the DB to its normal (fully migrated) schema so
        # later tests in this run see the real, current meso schema.
        executor.loader.build_graph()
        executor.migrate(leaf_nodes)
