from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Event(models.Model):
    """One first-party usage event (#509).

    Written only through ``analytics.track.track()``, which owns the closed
    set of names and the sandbox/staff exclusion. No IP address, user agent
    or request path is stored: anything an event needs goes in ``props``.

    ``actor`` is ``SET_NULL`` for the same reason as ``meso.TourEvent.coach``:
    counts must survive a user's deletion rather than vanish with the row.
    ``subject_type``/``subject_id`` are plain strings (``"meso.plan"``,
    ``"42"``), not a GenericForeignKey. Subjects have both UUID and integer
    pks, and nothing here needs to join back through the ORM.
    """

    class Source(models.TextChoices):
        SERVER = "server", _("Server")
        CLIENT = "client", _("Client")

    name = models.CharField(_("Name"), max_length=64, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_events",
        verbose_name=_("Actor"),
    )
    subject_type = models.CharField(_("Subject type"), max_length=64, blank=True)
    subject_id = models.CharField(_("Subject id"), max_length=64, blank=True)
    props = models.JSONField(
        _("Properties"), default=dict, blank=True, encoder=DjangoJSONEncoder
    )
    source = models.CharField(
        _("Source"), max_length=8, choices=Source, default=Source.SERVER
    )
    created = models.DateTimeField(_("Created"), default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(
                fields=["name", "created"], name="analytics_event_name_created"
            ),
        ]
        verbose_name = _("event")
        verbose_name_plural = _("events")

    def __str__(self):
        return f"{self.name} @ {self.created:%Y-%m-%d %H:%M}"
