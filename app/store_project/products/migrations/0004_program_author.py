# Generated by Django 3.1 on 2020-08-30 15:51

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("products", "0003_auto_20200830_1503"),
    ]

    operations = [
        migrations.AddField(
            model_name="program",
            name="author",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
                verbose_name="Author of product",
            ),
        ),
    ]
