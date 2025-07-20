from django.db import migrations, models
import store_project.challenges.models


class Migration(migrations.Migration):
    dependencies = [
        ("challenges", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="challenge",
            name="difficulty_level",
            field=models.CharField(
                default=store_project.challenges.models.DifficultyLevel.BEGINNER,
                max_length=20,
                choices=list(store_project.challenges.models.DifficultyLevel.choices),
            ),
        ),
    ]
