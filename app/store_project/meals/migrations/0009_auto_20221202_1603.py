# Generated by Django 3.2 on 2022-12-02 16:03

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('meals', '0008_ingredient_spoon_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='meal',
            name='name',
            field=models.CharField(blank=True, default=None, max_length=100),
        ),
        migrations.AlterField(
            model_name='meal',
            name='cals',
            field=models.PositiveSmallIntegerField(blank=True, default=None),
        ),
        migrations.AlterField(
            model_name='meal',
            name='carbs',
            field=models.PositiveSmallIntegerField(blank=True, default=None),
        ),
        migrations.AlterField(
            model_name='meal',
            name='description',
            field=models.TextField(blank=True, default=None, max_length=2000),
        ),
        migrations.AlterField(
            model_name='meal',
            name='fat',
            field=models.PositiveSmallIntegerField(blank=True, default=None),
        ),
        migrations.AlterField(
            model_name='meal',
            name='fiber',
            field=models.PositiveSmallIntegerField(blank=True, default=None),
        ),
        migrations.AlterField(
            model_name='meal',
            name='net_cals',
            field=models.PositiveSmallIntegerField(blank=True, default=None),
        ),
        migrations.AlterField(
            model_name='meal',
            name='net_carbs',
            field=models.PositiveSmallIntegerField(blank=True, default=None),
        ),
        migrations.AlterField(
            model_name='meal',
            name='protein',
            field=models.PositiveSmallIntegerField(blank=True, default=None),
        ),
    ]