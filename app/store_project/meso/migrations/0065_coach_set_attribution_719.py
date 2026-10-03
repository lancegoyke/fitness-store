from django.db import migrations, models


def backfill_entered_by_coach(apps, schema_editor):
    # #722 has written coach sets since it deployed, without this flag; prod
    # had 0 such rows when this was written.
    LoggedSet = apps.get_model("meso", "LoggedSet")
    LoggedSet.objects.filter(
        source_line__athlete_authored=True, source_line__entered_by_coach=True
    ).update(entered_by_coach=True)


class Migration(migrations.Migration):
    # Additive only: both columns carry a `db_default`, so old code inserting
    # during a rolling deploy (in either direction) keeps working.

    dependencies = [
        ("meso", "0064_prescription_entered_by_coach_709"),
    ]

    operations = [
        migrations.AddField(
            model_name="loggedset",
            name="entered_by_coach",
            field=models.BooleanField(
                db_default=False, default=False, verbose_name="Entered by coach"
            ),
        ),
        migrations.AddField(
            model_name="sessionlog",
            name="opened_by_coach",
            field=models.BooleanField(
                db_default=False, default=False, verbose_name="Opened by coach"
            ),
        ),
        migrations.RunPython(backfill_entered_by_coach, migrations.RunPython.noop),
    ]
