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
