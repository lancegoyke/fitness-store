# Generated by Django 3.2.11 on 2022-08-27 15:08

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('products', '0017_auto_20220827_1505'),
    ]

    operations = [
        migrations.AddField(
            model_name='price',
            name='content_type',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='contenttypes.contenttype'),
        ),
        migrations.AddField(
            model_name='price',
            name='object_id',
            field=models.CharField(max_length=50, null=True),
        ),
        migrations.AddIndex(
            model_name='price',
            index=models.Index(fields=['content_type', 'object_id'], name='products_pr_content_cc428b_idx'),
        ),
    ]
