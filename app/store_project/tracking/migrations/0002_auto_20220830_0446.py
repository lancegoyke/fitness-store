# Generated by Django 3.2 on 2022-08-30 04:46

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('tracking', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='test',
            name='video_link',
            field=models.URLField(blank=True, default=None),
        ),
        migrations.CreateModel(
            name='Measure',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('value', models.PositiveIntegerField()),
                ('unit', models.CharField(choices=[('sec', 'Seconds'), ('lb', 'Pounds'), ('kg', 'Kilograms'), ('mi', 'Miles'), ('m', 'Meters'), ('ft', 'Feet'), ('in', 'Inches'), ('yd', 'Yards'), ('W', 'Watts')], max_length=3)),
                ('created', models.DateTimeField(auto_now_add=True, verbose_name='Time created')),
                ('modified', models.DateTimeField(auto_now=True, verbose_name='Time last modified')),
                ('test', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='tracking.test')),
                ('user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]