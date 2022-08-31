# Generated by Django 3.2 on 2022-08-31 04:13

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('tracking', '0004_auto_20220830_1619'),
    ]

    operations = [
        migrations.AlterField(
            model_name='test',
            name='content_type',
            field=models.ForeignKey(default=None, limit_choices_to=models.Q(('model', 'LoadMeasure'), ('model', 'PowerMeasure'), ('model', 'DistanceMeasure'), ('model', 'DurationMeasure'), _connector='OR'), null=True, on_delete=django.db.models.deletion.CASCADE, to='contenttypes.contenttype'),
        ),
    ]
