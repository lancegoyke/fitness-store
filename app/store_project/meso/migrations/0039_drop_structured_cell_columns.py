# Phase 2a step 2 of 2: drop the retired structured cell columns.
#
# Split out of ``0038_text_first_cells`` because Postgres cannot ALTER TABLE
# (DROP COLUMN) in the same transaction as that migration's data writes — the
# sub-line ``bulk_create`` leaves pending FK trigger events on
# ``meso_prescription``. 0038 composes every cell's text while both column
# sets exist; this one removes the old set.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("meso", "0038_text_first_cells"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="prescription",
            name="load",
        ),
        migrations.RemoveField(
            model_name="prescription",
            name="load_type",
        ),
        migrations.RemoveField(
            model_name="prescription",
            name="note",
        ),
        migrations.RemoveField(
            model_name="prescription",
            name="reps",
        ),
        migrations.RemoveField(
            model_name="prescription",
            name="rest",
        ),
        migrations.RemoveField(
            model_name="prescription",
            name="rpe",
        ),
        migrations.RemoveField(
            model_name="prescription",
            name="sets",
        ),
        migrations.RemoveField(
            model_name="prescription",
            name="swap_exercise",
        ),
        migrations.RemoveField(
            model_name="prescription",
            name="swap_name",
        ),
    ]
