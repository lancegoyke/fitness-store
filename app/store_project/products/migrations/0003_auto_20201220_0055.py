# Generated by Django 3.1.2 on 2020-12-20 00:55

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("products", "0002_auto_20201121_2010"),
    ]

    operations = [
        migrations.AlterField(
            model_name="program",
            name="program_file",
            field=models.FileField(
                blank=True,
                null=True,
                upload_to="products/programs/",
                verbose_name="File containing program",
            ),
        ),
    ]
