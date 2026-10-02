# #578 stage 4c: drop the ``meso_loggedset.reclaimed_line_id`` column.
#
# 0059 (stage 4b) already removed ``LoggedSet.reclaimed_line`` from the model
# state and left the column behind, because a deploy runs ``migrate`` before it
# swaps containers and the stage 4a code still selected it. 4b is live now, so
# no running code reads or writes the column, and this drops it from the
# database. Database-only: the model state has nothing left to change.
#
# The column was a ``ForeignKey(db_constraint=False)``, so there is no FK
# constraint to drop, only the index Django created for it. Postgres drops that
# index with the column. SQLite refuses to drop an indexed column, so the index
# goes first there. The index name is Django's deterministic one for this
# table and column; it is the same on both backends (checked on prod Postgres).
#
# Reverse re-adds the column as nullable with its index. Its values are not
# restored; nothing has read them since 4b.
from django.db import migrations

TABLE = "meso_loggedset"
COLUMN = "reclaimed_line_id"
INDEX = "meso_loggedset_reclaimed_line_id_b346a9b7"


def drop_column(apps, schema_editor):
    if schema_editor.connection.vendor == "sqlite":
        schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX}")
        schema_editor.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
    else:
        schema_editor.execute(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {COLUMN}")


def add_column(apps, schema_editor):
    schema_editor.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} integer NULL")
    schema_editor.execute(f"CREATE INDEX {INDEX} ON {TABLE} ({COLUMN})")


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0059_loggedset_reclaimed_line_state_only"),
    ]

    operations = [
        migrations.RunPython(drop_column, add_column, elidable=False),
    ]
