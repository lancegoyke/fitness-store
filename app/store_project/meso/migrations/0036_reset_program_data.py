from django.db import migrations


def reset_program_data(apps, schema_editor):
    """Pre-launch reshape (no data to preserve): delete every ``Plan`` so the
    fixed-lineup schema ops (the NEXT migration) apply to empty program tables AND
    the demo rebuilds cleanly post-deploy.

    Deleting the plans cascades the WHOLE program hierarchy — mesocycles → weeks →
    sessions → prescriptions/logs/deliveries, agent batches → proposed changes, and
    the designer undo/redo ``PlanAction`` stacks — while leaving the coach/athlete/
    group/subscription scaffolding intact (``Plan`` FKs *to* those; they are not
    cascaded). This matters because:

    - the new NOT NULL ``Session.session_slot`` FK and the repointed ``prescription``
      FKs would otherwise violate their constraints against pre-existing rows;
    - ``seed_meso_demo`` early-returns when ``plan.mesocycles.exists()`` and the group
      path only builds when the shared plan is missing, so leaving empty
      ``Mesocycle``/``Week`` shells behind would leave delivered-but-empty programs the
      reseeder never rebuilds — deleting the plans resets to the fresh-DB state the
      reseeder is built to populate (idempotent), which recreates the new-shape tree;
    - stale ``PlanAction`` snapshots reference the retired per-week rows, so an undo
      after the cutover would restore an old-shape snapshot with no slots/cells — the
      cascade drops those stacks with their plans.

    This reset lives in its OWN migration (separate from the schema cutover in
    ``0037``) on purpose: on Postgres a cascade DELETE queues deferred RI trigger
    events, and ``ALTER TABLE`` refuses to run against a table with pending trigger
    events *in the same transaction* ("cannot ALTER TABLE ... because it has pending
    trigger events"). Committing this migration first flushes those events, so the
    ALTERs in ``0037`` land on clean, empty tables. (SQLite has no such rule, which
    is why the combined form passed CI but failed the prod migrate.)

    On a fresh DB there are no plans, so this is a no-op. Reseed with
    ``manage.py seed_meso_demo`` after the deploy. Irreversible (reverse = no-op).
    """
    apps.get_model("meso", "Plan").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('meso', '0035_tourevent'),
    ]

    operations = [
        migrations.RunPython(reset_program_data, migrations.RunPython.noop),
    ]
