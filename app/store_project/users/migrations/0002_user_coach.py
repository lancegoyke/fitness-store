# Generated by Django 3.2.11 on 2022-08-19 17:24

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0001_squashed_0002_auto_20201121_1727'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='coach',
            field=models.BooleanField(null=True, verbose_name='Is the user a coach?'),
        ),
    ]
