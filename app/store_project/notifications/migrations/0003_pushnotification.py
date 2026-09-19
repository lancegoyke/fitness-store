"""Create ``PushNotification``, the push ledger (#509 slice 3).

The push peer of the ``SentEmail``/``EmailEvent`` tables ``0001_initial``
created: one row per web push actually sent to one subscription, written by
``notifications.push.log_push_sent`` before the send goes out and updated by
``log_push_error``/``record_push_click`` afterwards. See ``PushNotification``'s
own docstring in ``models.py`` for why it's a dedicated table rather than a
generic ``Notification`` with ``channel=push``, and why its primary key is a
UUID rather than the default auto-incrementing int.

No ``django_q`` dependency: unlike the retention sweep in
``analytics/migrations/0002_register_event_retention_schedule.py``, this slice
adds no new ``Schedule`` row — the daily ``analytics_purge_events`` management
command already sweeps this table too (see ``notifications.retention``).
"""

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0002_emailkind_store_kinds"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PushNotification",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("block_delivered", "Block delivered"),
                            ("other", "Other"),
                        ],
                        db_index=True,
                        default="other",
                        max_length=32,
                    ),
                ),
                (
                    "sent_at",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now
                    ),
                ),
                ("error", models.CharField(blank=True, max_length=255)),
                (
                    "clicked_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="push_notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Push notification",
                "verbose_name_plural": "Push notifications",
                "ordering": ["-sent_at"],
                "indexes": [
                    models.Index(
                        fields=["kind", "sent_at"], name="notificatio_kind_4404e9_idx"
                    )
                ],
            },
        ),
    ]
