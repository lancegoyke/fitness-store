"""The closed set of first-party event names (#509).

Every ``track()`` call names one of these. ``track()`` rejects anything else,
so a typo can't quietly start a new event nobody queries. Adding an event
means adding it here, next to its siblings.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class EventName(models.TextChoices):
    # Coach: building and delivering programs.
    PLAN_CREATED = "plan_created", _("Plan created")
    TEMPLATE_IMPORTED = "template_imported", _("Template imported")
    AGENT_PROPOSAL_RUN = "agent_proposal_run", _("Agent proposal run")
    BATCH_APPLIED = "batch_applied", _("Agent batch applied")
    BLOCK_DELIVERED = "block_delivered", _("Block delivered")

    # Athlete: training.
    SESSION_OPENED = "session_opened", _("Session opened")
    SET_LOGGED = "set_logged", _("Set logged")
    SESSION_COMPLETED = "session_completed", _("Session completed")

    # Relationships.
    INVITE_SENT = "invite_sent", _("Invite sent")
    INVITE_ACCEPTED = "invite_accepted", _("Invite accepted")
    COACH_REQUEST_SENT = "coach_request_sent", _("Coach request sent")

    # Billing.
    SUBSCRIPTION_STARTED = "subscription_started", _("Subscription started")
    SUBSCRIPTION_CANCELLED = "subscription_cancelled", _("Subscription cancelled")

    # Devices.
    PUSH_SUBSCRIBED = "push_subscribed", _("Push subscribed")

    # Browser-only moments, recorded through the client beacon (#509 slice 3).
    # These are the facts no server request reveals: whether the athlete
    # installed the app, and what they answered when the browser asked about
    # notifications. `push_clicked` is the exception in this group — the server
    # writes it from the notification's landing URL (see
    # `notifications.push.record_push_click`), and the beacon accepts it only
    # so the whole browser-only set has one home.
    PWA_INSTALLED = "pwa_installed", _("PWA installed")
    PUSH_PERMISSION = "push_permission", _("Push permission answered")
    PUSH_CLICKED = "push_clicked", _("Push notification clicked")
