# Generated by Django 3.2 on 2022-08-31 15:15

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tracking', '0007_rename_test_content_type_test_measurement_content_type'),
    ]

    operations = [
        migrations.RenameField(
            model_name='test',
            old_name='measurement_content_type',
            new_name='measurement_type',
        ),
    ]