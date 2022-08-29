# Generated by Django 3.2.11 on 2022-08-26 18:07

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0014_auto_20220826_1804'),
    ]

    operations = [
        migrations.AlterField(
            model_name='price',
            name='billing_scheme',
            field=models.CharField(choices=[('tiered', '`tiered`: unit pricing will be computed using a tiered strategy defined with `tiers` and `tiers_mode`'), ('per_unit', '`per_unit`: the fixed amount will be charged per unit in `quantity` (for prices with `usage_type=licensed`) or per unit of total usage (for prices with `usage_type=metered`)')], default='per_unit', help_text='How to compute the price per period', max_length=8),
        ),
        migrations.AlterField(
            model_name='price',
            name='price_type',
            field=models.CharField(choices=[('one_time', 'One Time'), ('recurring', 'Recurring')], default='one_time', max_length=9),
        ),
        migrations.AlterField(
            model_name='price',
            name='tax_behavior',
            field=models.CharField(choices=[('inclusive', 'The price includes tax payment'), ('exclusive', 'The price does not include tax payment'), ('unspecified', 'Tax behavior not specified')], default='unspecified', max_length=11),
        ),
    ]
