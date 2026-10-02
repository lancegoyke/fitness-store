# #578 stage 4b: drop ``LoggedSet.reclaimed_line`` from the MODEL only.
#
# The column stays in the database until stage 4c. A deploy runs ``migrate``
# before it swaps containers, so the old (stage 4a) code, which still selects
# ``reclaimed_line``, would hit a missing column if this dropped it. The column
# is nullable with no database FK (``db_constraint=False``), so INSERTs from the
# new code that omit it simply get NULL.
#
# Once the field is gone Django no longer applies its Python-side SET_NULL, so
# deleting a ``Prescription`` can leave a stale id in the dead column. That is
# harmless: nothing reads it, and 4c drops the column.
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0058_invite_accepted_643"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="loggedset", name="reclaimed_line"),
            ],
            database_operations=[],
        ),
    ]
