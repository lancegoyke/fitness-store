"""Guard: no project migration may depend on another app's ``__latest__``.

``__latest__`` is re-resolved to the target app's leaf migration every time
the migration graph is built. If that app (e.g. django-q2, a third-party
dependency) ships a new migration, an already-applied migration in this
project silently starts depending on a migration that has never run, and
``migrate`` raises ``InconsistentMigrationHistory`` (#552). Every
cross-app dependency must instead be pinned to a concrete migration name.

This scans every app under ``store_project``, not just meso — the bug isn't
meso-specific, it's wherever a migration reaches into another app's graph.
"""

from django.db.migrations.loader import MigrationLoader


def test_no_project_migration_depends_on_latest_of_another_app():
    """Fail and name every offender if any project migration uses `__latest__`."""
    loader = MigrationLoader(None, ignore_no_migrations=True)

    offenders = []
    for (app_label, migration_name), migration in loader.disk_migrations.items():
        module = type(migration).__module__
        if not module.startswith("store_project."):
            continue
        for dep_app, dep_name in migration.dependencies:
            if dep_name == "__latest__":
                offenders.append(
                    f"{app_label}.{migration_name} -> ('{dep_app}', '__latest__')"
                )

    assert not offenders, (
        "Migrations must pin cross-app dependencies to a concrete migration "
        "name, not '__latest__' (it re-resolves every graph build and can "
        "break `migrate` once the target app ships a new migration, #552):\n"
        + "\n".join(sorted(offenders))
    )
