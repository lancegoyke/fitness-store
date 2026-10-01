from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0055_loggedset_unit"),
    ]

    operations = [
        migrations.AlterField(
            model_name="coachprofile",
            name="default_unit",
            field=models.CharField(
                choices=[("kg", "Kilograms"), ("lb", "Pounds")],
                default="lb",
                max_length=2,
                verbose_name="Default unit",
            ),
        ),
    ]
