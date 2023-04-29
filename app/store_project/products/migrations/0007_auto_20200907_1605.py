# Generated by Django 3.1 on 2020-09-07 16:05

import markdownx.models
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("products", "0006_program_page_content"),
    ]

    operations = [
        migrations.AddField(
            model_name="program",
            name="price",
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=10, verbose_name="Price"
            ),
        ),
        migrations.AlterField(
            model_name="program",
            name="page_content",
            field=markdownx.models.MarkdownxField(
                blank=True, default="", verbose_name="Page content, in markdown"
            ),
        ),
    ]
