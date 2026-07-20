"""Tenancy, roles, and the coach↔athlete relationship spine for Meso.

This is the first real (DB-backed) slice of the Meso program designer, which
once ran entirely on a fixtures module (since retired now every screen is
DB-backed). See ``docs/archive/meso/persistence-plan.md`` (Phase 1) and
``docs/meso/decisions.md``.

Multi-coach SaaS (B1): every coach is an account, athletes are Users who log in
(B2), and the link between them is a many-to-many, athlete-consented
relationship (N1). Roles are marked by the presence of a ``CoachProfile`` /
``AthleteProfile`` rather than flags on the User model (N3).
"""

import uuid
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Unit(models.TextChoices):
    KILOGRAMS = "kg", _("Kilograms")
    POUNDS = "lb", _("Pounds")


class InvalidTransition(Exception):
    """Raised when a CoachAthlete state-machine transition is not allowed."""


class CoachProfile(models.Model):
    """Marks a User as a coach; the presence of a row *is* being a coach (N3).

    Holds the coach's programming voice — the style tags and avoid-rules that
    the designer surfaces and (later) the agent is grounded on.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coach_profile",
        verbose_name=_("User"),
    )
    display_name = models.CharField(_("Display name"), max_length=255, blank=True)
    programming_style = models.JSONField(
        _("Programming style"),
        default=list,
        blank=True,
        help_text=_(
            "List of short style tags, e.g. ['Compound-first', 'RPE-based load']."
        ),
    )
    avoid_rules = models.TextField(_("Avoid rules"), blank=True)
    default_unit = models.CharField(
        _("Default unit"), max_length=2, choices=Unit, default=Unit.KILOGRAMS
    )
    tour_state = models.JSONField(
        _("Guided tour state"),
        default=dict,
        blank=True,
        help_text=_(
            "Guided demo onboarding tour progress (issue #430): "
            "{'step': <int>, 'status': 'active'|'dismissed'|'completed'}. "
            "An empty dict means the tour has never started."
        ),
    )
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)

    class Meta:
        verbose_name = "Coach profile"
        verbose_name_plural = "Coach profiles"

    def __str__(self):
        return self.display_name or self.user.display_name()


class SandboxSession(models.Model):
    """Marks a throwaway demo-sandbox coach account (issue #389).

    A logged-out visitor to ``/meso/demo/`` gets a real, logged-in throwaway
    coach ``User`` so every existing login-gated view / CSRF / scoping query
    just works (``docs/meso/public-sandbox-demo-plan.md``). This row is the
    marker the view-layer guards and the eventual expiry sweep key off —
    distinct from ``is_demo`` (relationship-scoped demo *data*, not a
    user-scoped sandbox *account*).
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sandbox_session",
    )
    created = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)

    def __str__(self):
        return f"Sandbox session for {self.user_id} (expires {self.expires_at})"


class TourEvent(models.Model):
    """One row per guided-tour funnel moment (issue #430 Phase 4 — analytics + polish).

    The ``analytics`` app is Google-Analytics-script-only — a context
    processor handing a GA property id to the template, no server-side event
    model at all (``store_project/analytics/context_processors.py``). A
    client-side GA/JS beacon would also fail the plan's own requirement that
    the funnel "can't be ad-blocked away". So this is a minimal, meso-local
    events table instead, recorded server-side at the tour's own endpoints
    (``tour.py``'s ``record_*`` helpers): one insert per event, no reads in
    the hot path. There's no dashboard yet — the owner reads this via the
    Django admin or a shell query for now (a follow-up).

    ``coach`` is ``SET_NULL`` (mirrors e.g. ``ExercisePrescription.exercise``):
    most tour activity happens on throwaway sandbox coaches the hourly expiry
    sweep deletes (``sandbox.expire_sandboxes``), and the funnel counts must
    survive that reap rather than vanishing with the row — a cascade here
    would silently erase most of the data this table exists to keep.
    """

    class Kind(models.TextChoices):
        STARTED = "started", _("Started")
        ADVANCED = "advanced", _("Step advanced")
        OPT_IN = "opt_in", _("Segment/self-action opt-in")
        DISMISSED = "dismissed", _("Dismissed")
        COMPLETED = "completed", _("Completed")
        SKIPPED = "skipped", _("Skipped (load everything)")

    class Variant(models.TextChoices):
        SANDBOX = "sandbox", _("Sandbox")
        SELF = "self", _("Real coach (self-coaching)")

    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tour_events",
        verbose_name=_("Coach"),
    )
    kind = models.CharField(_("Kind"), max_length=16, choices=Kind)
    variant = models.CharField(_("Variant"), max_length=8, choices=Variant)
    step_key = models.CharField(
        _("Step key"),
        max_length=32,
        blank=True,
        help_text=_("The tour.STEPS key this event happened at/for, e.g. 'designer'."),
    )
    segment = models.CharField(
        _("Segment / action"),
        max_length=32,
        blank=True,
        help_text=_(
            "For opt_in events only: the demo segment (athletes/program/"
            "delivery/log) or self-variant action (roster_add_self/"
            "plan_create) that was opted into."
        ),
    )
    created = models.DateTimeField(_("Time created"), auto_now_add=True)

    class Meta:
        verbose_name = "Tour event"
        verbose_name_plural = "Tour events"
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["kind", "created"]),
            models.Index(fields=["coach", "created"]),
        ]

    def __str__(self):
        detail = self.step_key or self.segment or "—"
        return f"{self.get_kind_display()} ({self.variant}) · {detail}"


class AthleteProfile(models.Model):
    """Cross-coach attributes that belong to the athlete, not to any one plan (D-b).

    Goals/focus live per-plan (added in a later slice); training history and
    contraindications are global to the athlete and visible to every coach.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="athlete_profile",
        verbose_name=_("User"),
    )
    training_started = models.DateField(_("Training started"), null=True, blank=True)
    notes = models.TextField(_("Notes"), blank=True)
    delivery_email_opt_out = models.BooleanField(
        _("Opted out of delivery emails"),
        default=False,
        help_text=_(
            "Set when the athlete unsubscribes from training-delivery emails "
            "(the email's List-Unsubscribe link). Web push is unaffected."
        ),
    )
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)

    class Meta:
        verbose_name = "Athlete profile"
        verbose_name_plural = "Athlete profiles"

    def __str__(self):
        return self.user.display_name()

    @property
    def training_months(self):
        """Whole months of training experience, or None if unknown."""
        if not self.training_started:
            return None
        today = timezone.localdate()
        months = (today.year - self.training_started.year) * 12 + (
            today.month - self.training_started.month
        )
        if today.day < self.training_started.day:
            months -= 1
        return max(months, 0)


class Contraindication(models.Model):
    """An injury/limitation that is global to the athlete — every coach sees it (D-b)."""

    athlete = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="contraindications",
        verbose_name=_("Athlete"),
    )
    text = models.CharField(_("Contraindication"), max_length=255)
    active = models.BooleanField(_("Active"), default=True)
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Contraindication"
        verbose_name_plural = "Contraindications"

    def __str__(self):
        return self.text

    @property
    def label(self):
        """Short badge form — the part before an em/en dash, e.g. 'L knee'."""
        return self.text.split("—")[0].split("–")[0].split(" - ")[0].strip()


class CoachAthleteQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status=CoachAthlete.Status.ACTIVE)

    def billable(self):
        """Active links that count as a paid **seat** — excluding demo and self ones.

        A demo athlete (the one-click first-run demo, ``meso/demo.py``) is a real
        active link the coach manages on their roster, but it must not consume a
        paid seat or trip the paywall — so the billing accessors count seats off
        this, not ``active``. A coach's **self-link** (``is_self`` — programming
        for themselves, guided-tour Phase 0) is free for the same reason: it must
        never be what pushes them onto a paid plan.
        """
        return self.active().exclude(is_demo=True).exclude(is_self=True)

    def pending(self):
        return self.filter(status__in=CoachAthlete.PENDING_STATUSES)

    def closed(self):
        """Terminal-state links — declined or ended (the relationship history).

        These vanish from the active roster (only ``active()`` is shown), but the
        row + its ``ended_at``/``responded_at`` timestamp + the archived plans all
        persist, so a coach can review past athletes and re-invite them.
        """
        return self.filter(status__in=CoachAthlete.CLOSED_STATUSES)

    def for_coach(self, user):
        """Links where ``user`` is the coach (i.e. the coach's athletes)."""
        return self.filter(coach=user)

    def for_athlete(self, user):
        """Links where ``user`` is the athlete (i.e. the athlete's coaches)."""
        return self.filter(athlete=user)


class CoachAthlete(models.Model):
    """The load-bearing relationship: a coach programming for an athlete (N1).

    Many-to-many and athlete-consented — an athlete may work with several
    coaches, and either party can end the link. Relationships require the other
    party's acceptance, so a row carries a small state machine (see ``invite`` /
    ``request`` / ``accept`` / ``decline`` / ``end``).
    """

    class Status(models.TextChoices):
        PENDING_COACH_INVITE = "pending_coach_invite", _("Pending athlete acceptance")
        PENDING_ATHLETE_REQUEST = (
            "pending_athlete_request",
            _("Pending coach acceptance"),
        )
        ACTIVE = "active", _("Active")
        DECLINED = "declined", _("Declined")
        ENDED = "ended", _("Ended")

    class InvitedBy(models.TextChoices):
        COACH = "coach", _("Coach")
        ATHLETE = "athlete", _("Athlete")

    PENDING_STATUSES = (Status.PENDING_COACH_INVITE, Status.PENDING_ATHLETE_REQUEST)
    # Statuses a fresh invite/request may reopen from.
    CLOSED_STATUSES = (Status.DECLINED, Status.ENDED)

    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="athlete_links",
        verbose_name=_("Coach"),
    )
    athlete = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coach_links",
        verbose_name=_("Athlete"),
    )
    status = models.CharField(_("Status"), max_length=32, choices=Status.choices)
    invited_by = models.CharField(
        _("Invited by"), max_length=8, choices=InvitedBy.choices
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # A demo relationship (the coach-scoped first-run demo, ``meso/demo.py``):
    # shown on the roster like any athlete, but not a billable seat and removable
    # in one click. Real relationships are never demo.
    is_demo = models.BooleanField(_("Demo relationship"), default=False)
    # A self-coaching relationship (guided-tour Phase 0): the coach on their own
    # roster, programming for themselves. A real link — plans, delivery, and
    # logging all work against the coach's real account — but never a billable
    # seat (mirroring ``is_demo``). The only row allowed with coach == athlete,
    # and ``unique(coach, athlete)`` caps a user at one.
    is_self = models.BooleanField(_("Self-coaching relationship"), default=False)
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)
    responded_at = models.DateTimeField(_("Time responded"), null=True, blank=True)
    ended_at = models.DateTimeField(_("Time ended"), null=True, blank=True)

    objects = CoachAthleteQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Coach-athlete link"
        verbose_name_plural = "Coach-athlete links"
        constraints = [
            models.UniqueConstraint(
                fields=["coach", "athlete"], name="unique_coach_athlete"
            ),
            # coach == athlete iff ``is_self`` — a self-link is the one sanctioned
            # same-user row (guided-tour Phase 0), and ``is_self`` can never be
            # smuggled onto a two-party relationship.
            models.CheckConstraint(
                condition=(
                    models.Q(coach=models.F("athlete"), is_self=True)
                    | (~models.Q(coach=models.F("athlete")) & models.Q(is_self=False))
                ),
                name="coach_athlete_distinct_unless_self",
            ),
        ]

    def __str__(self):
        return f"{self.coach.display_name()} → {self.athlete.display_name()} ({self.status})"

    # -- state machine ----------------------------------------------------

    @classmethod
    def invite(cls, *, coach, athlete):
        """Coach invites an athlete → ``pending_coach_invite`` (awaiting athlete)."""
        return cls._open(
            coach=coach,
            athlete=athlete,
            status=cls.Status.PENDING_COACH_INVITE,
            invited_by=cls.InvitedBy.COACH,
        )

    @classmethod
    def request(cls, *, athlete, coach):
        """Athlete requests a coach → ``pending_athlete_request`` (awaiting coach)."""
        return cls._open(
            coach=coach,
            athlete=athlete,
            status=cls.Status.PENDING_ATHLETE_REQUEST,
            invited_by=cls.InvitedBy.ATHLETE,
        )

    @classmethod
    def add_self(cls, user):
        """The coach adds *themselves* as an athlete → immediately ``active``.

        Self-coaching (guided-tour Phase 0): there's no consent to gather from
        yourself, so the link skips the invite dance and lands straight on
        ``active`` with ``is_self`` set — the one row the same-user check
        constraint sanctions. Idempotent: ``unique(coach, athlete)`` caps a user
        at one self-link, an open one is returned unchanged, and a previously
        ended one reopens (its archived plans stay archived, like any re-invite).
        Never a billable seat (``billable()`` excludes ``is_self``).
        """
        link, created = cls.objects.get_or_create(
            coach=user,
            athlete=user,
            defaults={
                "status": cls.Status.ACTIVE,
                "invited_by": cls.InvitedBy.COACH,
                "is_self": True,
                "responded_at": timezone.now(),
            },
        )
        if not created and link.status != cls.Status.ACTIVE:
            link.status = cls.Status.ACTIVE
            link.responded_at = timezone.now()
            link.ended_at = None
            link.save(update_fields=["status", "responded_at", "ended_at"])
        return link

    @classmethod
    def _open(cls, *, coach, athlete, status, invited_by):
        """Create the link, or reopen a previously declined/ended one.

        ``unique(coach, athlete)`` means there is at most one row per pair, so a
        re-invite reopens the existing row with a fresh token and status. An
        already-active or already-pending link is returned unchanged.
        """
        if coach == athlete:
            raise InvalidTransition("A user cannot coach themselves.")
        link, created = cls.objects.get_or_create(
            coach=coach,
            athlete=athlete,
            defaults={"status": status, "invited_by": invited_by},
        )
        if not created and link.status in cls.CLOSED_STATUSES:
            link.status = status
            link.invited_by = invited_by
            link.token = uuid.uuid4()
            link.responded_at = None
            link.ended_at = None
            link.save(
                update_fields=[
                    "status",
                    "invited_by",
                    "token",
                    "responded_at",
                    "ended_at",
                ]
            )
        return link

    def accept(self):
        """Recipient accepts a pending link → ``active``."""
        if self.status not in self.PENDING_STATUSES:
            raise InvalidTransition(f"Cannot accept a link that is {self.status}.")
        self.status = self.Status.ACTIVE
        self.responded_at = timezone.now()
        self.save(update_fields=["status", "responded_at"])
        return self

    def decline(self):
        """Recipient declines a pending link → ``declined``."""
        if self.status not in self.PENDING_STATUSES:
            raise InvalidTransition(f"Cannot decline a link that is {self.status}.")
        self.status = self.Status.DECLINED
        self.responded_at = timezone.now()
        self.save(update_fields=["status", "responded_at"])
        return self

    def end(self):
        """Either party ends an active link → ``ended``.

        Ending archives this coach's plans for the athlete (never deletes), and
        leaves the athlete's other coaches untouched (D-c).
        """
        if self.status != self.Status.ACTIVE:
            raise InvalidTransition(f"Cannot end a link that is {self.status}.")
        self.status = self.Status.ENDED
        self.ended_at = timezone.now()
        self.save(update_fields=["status", "ended_at"])
        self.plans.exclude(status=Plan.Status.ARCHIVED).update(
            status=Plan.Status.ARCHIVED
        )
        return self

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def is_pending(self):
        return self.status in self.PENDING_STATUSES

    @property
    def is_closed(self):
        """Terminal state — declined or ended (a row the history surface shows)."""
        return self.status in self.CLOSED_STATUSES

    @property
    def closed_at(self):
        """When the link entered its terminal state, or ``None`` while open.

        An ended link carries ``ended_at``; a declined one carries ``responded_at``
        (``decline`` stamps it). Used to order and label the relationship history.
        """
        if self.status == self.Status.ENDED:
            return self.ended_at
        if self.status == self.Status.DECLINED:
            return self.responded_at
        return None

    def recipient(self):
        """The party whose acceptance the link is waiting on (None if not pending)."""
        if self.status == self.Status.PENDING_COACH_INVITE:
            return self.athlete
        if self.status == self.Status.PENDING_ATHLETE_REQUEST:
            return self.coach
        return None

    def initiator(self):
        """The party who opened a pending link (None if not pending).

        The mirror of ``recipient()``: a coach invite is initiated by the coach,
        an athlete request by the athlete. The initiator is who may *withdraw* a
        pending link, as the recipient is who may accept/decline it.
        """
        if self.status == self.Status.PENDING_COACH_INVITE:
            return self.coach
        if self.status == self.Status.PENDING_ATHLETE_REQUEST:
            return self.athlete
        return None

    def working_plan(self):
        """This relationship's current program (most-recent non-archived).

        The plan the designer reopens, or ``None`` when the coach hasn't built
        one yet.
        """
        return (
            self.plans.exclude(status=Plan.Status.ARCHIVED)
            .order_by("-modified")
            .first()
        )

    def create_plan(self, *, title="New program", goal="", unit=None, status=None):
        """Create an individual program rooted at this relationship, with a scaffold.

        A starter ``Plan`` plus ``Plan.scaffold``'s minimal-but-usable tree, so
        the designer opens onto an editable, deliverable grid. ``unit`` defaults
        to the coach's preferred unit; ``status`` to a draft.
        """
        if unit is None:
            profile = getattr(self.coach, "coach_profile", None)
            unit = profile.default_unit if profile else Unit.KILOGRAMS
        plan = Plan.objects.create(
            relationship=self,
            title=title,
            goal=goal,
            status=status or Plan.Status.DRAFT,
            unit=unit,
        )
        plan.scaffold()
        return plan


class CoachInviteQuerySet(models.QuerySet):
    def for_coach(self, user):
        """Invites this coach sent."""
        return self.filter(coach=user)

    def pending(self):
        return self.filter(status=CoachInvite.Status.PENDING)

    def claimable(self):
        """Pending invites whose TTL hasn't run out (a null clock never expires)."""
        return self.filter(status=CoachInvite.Status.PENDING).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=timezone.now())
        )

    def overdue(self):
        """Pending invites past their TTL — the sweep's input (excludes null clocks)."""
        return self.filter(
            status=CoachInvite.Status.PENDING,
            expires_at__lte=timezone.now(),
        )

    def outstanding(self):
        """Not-yet-answered invites the coach roster surfaces (pending **or** expired)."""
        return self.filter(
            status__in=[CoachInvite.Status.PENDING, CoachInvite.Status.EXPIRED]
        )

    def due_for_reminder(self):
        """Pending invites in their reminder window that haven't been nudged yet.

        A claim link nearing its TTL — within ``INVITE_REMINDER_LEAD`` of
        ``expires_at`` but not yet past due — for which no reminder has gone out.
        A null-clock legacy invite never expires, so never needs a reminder
        (excluded by the clock filter). The ``meso_remind_expiring_invites``
        sweep's input; ``mark_reminded`` stamps each so a later sweep skips it
        (one reminder per arming cycle).
        """
        now = timezone.now()
        return self.filter(
            status=CoachInvite.Status.PENDING,
            reminder_sent_at__isnull=True,
            expires_at__isnull=False,
            expires_at__gt=now,
            expires_at__lte=now + CoachInvite.INVITE_REMINDER_LEAD,
        )


class CoachInvite(models.Model):
    """A coach's email invitation to an athlete who may not have an account yet (N4).

    The pre-relationship onboarding artifact. ``CoachAthlete.athlete`` is a
    non-null FK, so it can't represent an invite to someone who isn't a ``User``
    yet — rather than make the load-bearing relationship model nullable, the email
    invite lives here. The coach invites an *email*; we send a tokened claim link;
    whoever follows it while authenticated calls ``accept`` to **materialize** —
    and immediately activate — a ``CoachAthlete`` link.

    Distinct from ``CoachAthlete.invite`` (a peer invite between two existing
    Users awaiting the recipient's acceptance): this is email-addressed and
    **bearer-token-claimed**. The claim link is a 122-bit secret delivered to the
    invited inbox; we do not require the claiming user's email to equal the
    invited email (email-only login coexists with social providers, so a new
    athlete may sign up under a different address). The coach sees who accepted on
    the roster and can ``end`` the link if it's wrong. See
    ``docs/archive/meso/invites-plan.md``.
    """

    #: How long a fresh claim link stays valid before it must be resent (N4 P3).
    INVITE_TTL = timedelta(days=14)

    #: How far ahead of ``expires_at`` an invite earns a reminder email (N4 P4).
    INVITE_REMINDER_LEAD = timedelta(days=3)

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACCEPTED = "accepted", _("Accepted")
        DECLINED = "declined", _("Declined")
        REVOKED = "revoked", _("Revoked")
        EXPIRED = "expired", _("Expired")

    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_invites",
        verbose_name=_("Coach"),
    )
    email = models.EmailField(_("Email"))
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(
        _("Status"), max_length=16, choices=Status.choices, default=Status.PENDING
    )
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="claimed_invites",
        verbose_name=_("Accepted by"),
    )
    accepted_link = models.ForeignKey(
        "CoachAthlete",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Accepted link"),
    )
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)
    responded_at = models.DateTimeField(_("Time responded"), null=True, blank=True)
    expires_at = models.DateTimeField(
        _("Expires at"),
        null=True,
        blank=True,
        help_text=_(
            "When the claim link stops working. Null = never expires "
            "(legacy invites predating the TTL)."
        ),
    )
    reminder_sent_at = models.DateTimeField(
        _("Reminder sent at"),
        null=True,
        blank=True,
        help_text=_(
            "When the expiry-reminder email went out. Null = not yet reminded "
            "this arming cycle; cleared on resend."
        ),
    )

    objects = CoachInviteQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Coach invite"
        verbose_name_plural = "Coach invites"
        constraints = [
            # At most one *pending* invite per (coach, email); a re-invite reuses
            # the open row. Closed invites (accepted/declined/revoked) don't block
            # a fresh one.
            models.UniqueConstraint(
                fields=["coach", "email"],
                condition=models.Q(status="pending"),
                name="unique_pending_coach_invite",
            ),
        ]

    def __str__(self):
        return f"{self.coach.display_name()} → {self.email} ({self.status})"

    @staticmethod
    def normalize_email(email):
        """Trim + lowercase so dedup and the partial-unique constraint agree."""
        return (email or "").strip().lower()

    def save(self, *args, **kwargs):
        self.email = self.normalize_email(self.email)
        super().save(*args, **kwargs)

    @classmethod
    def _default_expiry(cls):
        return timezone.now() + cls.INVITE_TTL

    @classmethod
    def open_for(cls, *, coach, email):
        """Open a pending invite for ``email``, reusing the coach's open row if any.

        Returns ``(invite, created)``. The email is normalized so a differently
        cased re-invite resolves to the same row (the partial-unique constraint
        enforces one pending invite per ``(coach, email)``). A fresh invite is
        stamped with a TTL (``_default_expiry``). An **outstanding** row — pending
        or expired — for the same address is reused rather than orphaned: if it's
        no longer claimable (expired or past due) it is *re-armed* (``resend`` —
        new token + reset clock); a still-live link is returned untouched. A
        previously *answered* invite (accepted/declined/revoked) does not block a
        fresh one.
        """
        email = cls.normalize_email(email)
        invite = (
            cls.objects.filter(coach=coach, email=email)
            .filter(status__in=[cls.Status.PENDING, cls.Status.EXPIRED])
            .order_by("-created_at")
            .first()
        )
        if invite is not None:
            if not invite.is_claimable:
                invite.resend()
            return invite, False
        # No outstanding row — create one. ``get_or_create`` keeps the create
        # race-safe against the partial-unique constraint.
        return cls.objects.get_or_create(
            coach=coach,
            email=email,
            status=cls.Status.PENDING,
            defaults={"expires_at": cls._default_expiry()},
        )

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING

    @property
    def is_expired(self):
        """True once the TTL has elapsed (a null clock never expires)."""
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_claimable(self):
        """A pending invite still within its TTL — the only state a claim accepts."""
        return self.is_pending and not self.is_expired

    @property
    def needs_reminder(self):
        """Single-invite mirror of ``due_for_reminder`` — is a reminder due now?

        A claimable invite (pending, not yet expired) with a real clock, nearing
        ``expires_at`` (within ``INVITE_REMINDER_LEAD``) and not yet reminded this
        cycle. A null clock never expires, so never qualifies.
        """
        if not self.is_claimable or self.reminder_sent_at is not None:
            return False
        if self.expires_at is None:
            return False
        return self.expires_at <= timezone.now() + self.INVITE_REMINDER_LEAD

    def mark_reminded(self):
        """Stamp that an expiry reminder went out (bookkeeping, not a transition).

        A reminded invite drops out of ``due_for_reminder`` so the sweep won't
        nudge it again this arming cycle; ``resend`` clears the stamp.
        """
        self.reminder_sent_at = timezone.now()
        self.save(update_fields=["reminder_sent_at"])
        return self

    def accept(self, user):
        """A claiming user accepts → an **active** ``CoachAthlete`` link.

        The claim *is* the athlete's acceptance, so the materialized link goes
        straight to active. Idempotent against an already-active link, and resolves
        a pre-existing pending peer link to active. Raises ``InvalidTransition`` if
        the invite is no longer pending, or if the claimer is the coach (a user
        cannot coach themselves).
        """
        if not self.is_pending:
            raise InvalidTransition(f"Cannot accept an invite that is {self.status}.")
        if self.is_expired:
            # The link's TTL ran out; flip it to expired and refuse — never
            # materialize a link from a stale token (the claim view's backstop).
            self.expire()
            raise InvalidTransition("Cannot accept an invite that has expired.")
        if user == self.coach:
            raise InvalidTransition("A coach cannot accept their own invite.")
        link = CoachAthlete.invite(coach=self.coach, athlete=user)
        if link.is_pending:
            link.accept()
        self.status = self.Status.ACCEPTED
        self.accepted_by = user
        self.accepted_link = link
        self.responded_at = timezone.now()
        self.save(
            update_fields=["status", "accepted_by", "accepted_link", "responded_at"]
        )
        return link

    def decline(self):
        """The recipient declines a pending invite → ``declined``."""
        if not self.is_pending:
            raise InvalidTransition(f"Cannot decline an invite that is {self.status}.")
        self.status = self.Status.DECLINED
        self.responded_at = timezone.now()
        self.save(update_fields=["status", "responded_at"])
        return self

    def revoke(self):
        """The coach cancels an outstanding invite → ``revoked``.

        Works on a pending **or expired** invite — a coach dismissing a dead
        invite off their roster is the same gesture as cancelling a live one. An
        already-answered invite (accepted/declined/revoked) can't be revoked.
        """
        if self.status not in (self.Status.PENDING, self.Status.EXPIRED):
            raise InvalidTransition(f"Cannot revoke an invite that is {self.status}.")
        self.status = self.Status.REVOKED
        self.responded_at = timezone.now()
        self.save(update_fields=["status", "responded_at"])
        return self

    def expire(self):
        """A past-due pending invite ages out → ``expired`` (the sweep / lazy aging).

        Distinct from ``revoke`` (a coach's deliberate cancel): expiry is the TTL
        running out. Only a pending invite that is actually past due can expire —
        the caller (``accept`` backstop, claim view, ``meso_expire_invites``)
        already knows it's overdue, but the guard keeps the transition honest.
        """
        if not self.is_pending:
            raise InvalidTransition(f"Cannot expire an invite that is {self.status}.")
        if not self.is_expired:
            raise InvalidTransition("Cannot expire an invite that is not past due.")
        self.status = self.Status.EXPIRED
        self.save(update_fields=["status"])
        return self

    def resend(self):
        """Re-arm an outstanding invite: a fresh token + a reset TTL.

        The explicit *resend* action — and the re-arm path ``open_for`` uses. A
        **new token** rotates the secret so the previously emailed link dies (per
        the Phase-3 decision); the clock resets and an expired invite returns to
        pending so it's claimable again. The reminder stamp is cleared so the
        fresh cycle re-earns a reminder near its new expiry. Only an outstanding
        invite (pending or expired) can be resent — an answered one
        (accepted/declined/revoked) is terminal; a fresh invite goes through
        ``open_for`` instead.
        """
        if self.status not in (self.Status.PENDING, self.Status.EXPIRED):
            raise InvalidTransition(f"Cannot resend an invite that is {self.status}.")
        self.token = uuid.uuid4()
        self.status = self.Status.PENDING
        self.expires_at = self._default_expiry()
        self.responded_at = None
        self.reminder_sent_at = None
        self.save(
            update_fields=[
                "token",
                "status",
                "expires_at",
                "responded_at",
                "reminder_sent_at",
            ]
        )
        return self


class CoachSubscription(models.Model):
    """A coach's billing state — a thin local mirror of Stripe (S6 billing, Phase 1).

    Meso is a multi-coach SaaS (B1); the coach pays (D1). This 1:1-with-the-coach
    row holds just enough to **gate a request without calling Stripe** (D8): the
    status, the Stripe ids (null until a card is actually collected), the mirrored
    period/trial clocks, and the last seat ``quantity`` synced to Stripe (a cache,
    not the truth). Stripe is the source of truth; this is the fast local read.

    Two product knobs gate off one predicate, ``is_active`` (D10): the **seat
    cap** (∞ active athletes when active, else ``FREE_SEAT_LIMIT``) and the **AI
    agent** (paid-only — the Claude agent has real per-call cost, so the free tier
    gets none). A ``comped`` status (D12) means the owner and the seeded demo
    coaches are never paywalled.

    The **trial is local** (D3): a no-card 14-day trial is just ``status=trialing``
    + a ``trial_end`` clock — Stripe is untouched until the coach actually
    subscribes. A lapsed trial reads inactive immediately (``is_active`` checks the
    clock); a Phase-2 qcluster sweep flips the persisted status back to ``free``.

    Phase 1 is this model + the ``billing/access.py`` accessor + the local trial +
    the comped seed/admin — **no Stripe and nothing enforced** (the Stripe
    Checkout/Portal/webhook land in Phase 2; the invite/agent choke points wire
    the gates in at Phase 3). See ``docs/meso/billing-plan.md``.
    """

    #: Free-tier seat cap — active athletes a non-paying coach may hold (open
    #: value, rec 1). Beyond this a free coach must start a trial or subscribe.
    FREE_SEAT_LIMIT = 1

    #: No-card local trial length, in days (open value, rec 14 — matches the
    #: invite TTL cadence).
    TRIAL_DAYS = 14

    #: Free-tier AI-agent allowance — agent runs a non-paying coach may make per
    #: calendar month (open value, set to 5). The Phase-5 metered refinement of the
    #: old binary free=no-agent gate (D4): a free coach gets a taste of the Claude
    #: agent before paying; beyond this the agent endpoint 402s. A run = an
    #: ``AgentProposalBatch`` (the batch table is the ledger — see ``billing/access.py``).
    FREE_AGENT_ALLOWANCE = 5

    #: Paid-tier (flat plan) AI-agent allowance — agent runs a trialing/active coach
    #: may make per calendar month (D14, set to 150). Under the **flat monthly Pro
    #: plan** the agent is the only per-run cost, so a generous-but-bounded cap keeps
    #: worst-case COGS per coach knowable (cap × ~$0.10) instead of open-ended; the
    #: agent-usage tracking measures real spend so this can be tuned from data. Only a
    #: ``comped`` coach (owner/demo) is uncapped. See ``docs/meso/billing-plan.md``.
    PAID_AGENT_ALLOWANCE = 150

    class Status(models.TextChoices):
        FREE = "free", _("Free")
        TRIALING = "trialing", _("Trialing")
        ACTIVE = "active", _("Active")
        PAST_DUE = "past_due", _("Past due")
        CANCELED = "canceled", _("Canceled")
        COMPED = "comped", _("Comped")

    #: Statuses that grant full (unlimited) access — the single gating predicate
    #: (a trialing row is only *really* active while its clock holds; see
    #: ``is_active``).
    ACTIVE_STATUSES = (Status.TRIALING, Status.ACTIVE, Status.COMPED)

    #: Statuses where a *real, current* Stripe subscription exists — active or
    #: temporarily past_due (a failed payment, not yet canceled). The local trial /
    #: free / comped tiers have no Stripe subscription, and ``canceled`` is a dead
    #: one (its ids are kept only for history). Used to decide whether to touch
    #: Stripe (seat sync) and to protect the tracked subscription from stale events
    #: for a different id.
    LIVE_STRIPE_STATUSES = (Status.ACTIVE, Status.PAST_DUE)

    coach = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coach_subscription",
        verbose_name=_("Coach"),
    )
    status = models.CharField(
        _("Status"), max_length=16, choices=Status.choices, default=Status.FREE
    )
    stripe_subscription_id = models.CharField(
        _("Stripe subscription id"),
        max_length=255,
        blank=True,
        default="",
        help_text=_("Null until the coach actually subscribes (enters a card)."),
    )
    stripe_item_id = models.CharField(
        _("Stripe subscription item id"),
        max_length=255,
        blank=True,
        default="",
        help_text=_("The per-seat subscription line item — for seat-quantity updates."),
    )
    stripe_base_item_id = models.CharField(
        _("Stripe base subscription item id"),
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "The flat base ($9.99/mo) line item, fixed at quantity 1 (S6 Phase 6). "
            "Seat sync only resizes the per-seat item, never this one."
        ),
    )
    trial_end = models.DateTimeField(
        _("Trial ends at"),
        null=True,
        blank=True,
        help_text=_("Set locally when the no-card trial starts; null = never trialed."),
    )
    current_period_end = models.DateTimeField(
        _("Current period ends at"),
        null=True,
        blank=True,
        help_text=_("Mirrored from Stripe for paid coaches."),
    )
    quantity = models.PositiveIntegerField(
        _("Synced seat quantity"),
        default=0,
        help_text=_("Last seat count synced to Stripe — a cache, not the truth."),
    )
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)

    class Meta:
        verbose_name = "Coach subscription"
        verbose_name_plural = "Coach subscriptions"

    def __str__(self):
        return f"{self.coach.display_name()} ({self.status})"

    # -- derived gating predicate ----------------------------------------

    @property
    def is_trial_expired(self):
        """True once a local trial's clock has run out (never fires off-trial).

        A null clock never expires (defensive — ``start_trial`` always sets one),
        mirroring the invite slice's "null clock never expires" semantics.
        """
        return (
            self.status == self.Status.TRIALING
            and self.trial_end is not None
            and self.trial_end <= timezone.now()
        )

    @property
    def is_active(self):
        """The single access predicate — full (unlimited) access right now.

        Trialing/active/comped grant access, **except** a trialing row whose clock
        has lapsed (the status flip to ``free`` is the Phase-2 sweep's job, but the
        gate is correct the instant the trial ends — lazy expiry).
        """
        return self.status in self.ACTIVE_STATUSES and not self.is_trial_expired

    # -- state machine ----------------------------------------------------

    def start_trial(self):
        """Begin the no-card local trial → ``trialing`` (free → trialing only).

        Single-use: a row that has ever trialed (``trial_end`` set, even after it
        lapsed back to ``free``) can't re-arm a second free trial. No Stripe is
        touched — the trial is pure local state until a card is collected.
        """
        if self.status != self.Status.FREE:
            raise InvalidTransition(f"Cannot start a trial from {self.status}.")
        if self.trial_end is not None:
            raise InvalidTransition("This coach has already used their free trial.")
        self.status = self.Status.TRIALING
        self.trial_end = timezone.now() + timedelta(days=self.TRIAL_DAYS)
        self.save(update_fields=["status", "trial_end", "modified"])
        return self

    def expire_trial(self):
        """A past-due local trial lapses → ``free`` (the Phase-2 sweep / lazy aging).

        Only a trialing row that is actually past due can expire; ``trial_end`` is
        preserved so the trial stays single-use (a lapsed coach can't re-trial).
        """
        if self.status != self.Status.TRIALING:
            raise InvalidTransition(f"Cannot expire a trial that is {self.status}.")
        if not self.is_trial_expired:
            raise InvalidTransition("Cannot expire a trial that is not past due.")
        self.status = self.Status.FREE
        self.save(update_fields=["status", "modified"])
        return self

    @classmethod
    def comp(cls, coach):
        """Mark a coach ``comped`` — unlimited, no Stripe (D12). Idempotent upsert.

        For the owner and seeded demo coaches, who are never paywalled.
        """
        sub, _ = cls.objects.update_or_create(
            coach=coach, defaults={"status": cls.Status.COMPED}
        )
        return sub

    @classmethod
    def start_trial_for(cls, coach):
        """Get-or-create the coach's row and start their trial (the view entry point)."""
        sub, _ = cls.objects.get_or_create(coach=coach)
        sub.start_trial()
        return sub


# ---------------------------------------------------------------------------
# Program schema (Phase 2; reshaped to a fixed lineup in the P0 schema cutover)
#
# The periodized plan a coach builds for an athlete:
#
#   Plan → Mesocycle → SessionSlot (fixed day)  ─┐
#                    → Week ────────────────────┼→ Session (week × day instance)
#                    → SessionSlot → ExerciseSlot (fixed row) → Prescription (cell = row × week)
#
# ``SessionSlot``/``ExerciseSlot`` are the mesocycle's fixed lineup — the day and
# exercise-row *identity*, shared across every week of the block. ``Prescription``
# is a per-week cell of freeform TEXT (Phase 2a spreadsheet parity: ``line`` 0 =
# the prescription, lines 1+ = optional sub-rows) plus the one-week ``skipped``
# exception; structure is derived on demand by ``parsing.parse_prescription``.
# Its resolving properties (``name``/``exercise``/``exercise_id``/``tags``)
# delegate to the slot, so read sites keep working unchanged.
# ``ExerciseSlot``/``Prescription`` together replace the old per-week
# ``ExercisePrescription`` row (retired). ``ExerciseSlot.exercise`` links to the
# catalog ``Exercise`` when one matches and falls back to free text otherwise
# (the B4 hybrid). Owned per coach↔athlete relationship (D-a) so each coach
# programs independently. The shape is driven by what the designer
# (``static/js/meso.js``) renders; see ``serializers.serialize_plan`` for the
# mapping back to that shape.
# ---------------------------------------------------------------------------


class PlanQuerySet(models.QuerySet):
    def for_coach(self, user):
        """Plans this coach owns through an *active* relationship (N2/D-a)."""
        return self.filter(
            relationship__coach=user,
            relationship__status=CoachAthlete.Status.ACTIVE,
        )

    def for_athlete(self, user):
        """Plans across all of the athlete's active coaches."""
        return self.filter(
            relationship__athlete=user,
            relationship__status=CoachAthlete.Status.ACTIVE,
        )

    def editable_by(self, user):
        """Plans this coach may open + edit in the designer.

        The designer + autosave surface: plans coached over an *active*
        relationship (``for_coach``), plus the coach's own template plans
        (parity plan §3.4 — a template is edited in the same grid, no second
        editor). Kept as its own gate because the designer endpoints all
        route through it.
        """
        return self.filter(
            models.Q(
                relationship__coach=user,
                relationship__status=CoachAthlete.Status.ACTIVE,
            )
            | models.Q(is_template=True, owner=user)
        )

    def active(self):
        return self.filter(status=Plan.Status.ACTIVE)


class Plan(models.Model):
    """A periodized training plan rooted at one coach↔athlete ``relationship`` (D-a).

    A **template** plan (parity plan §3.4) is a ``Plan`` with
    ``is_template=True``, no ``relationship`` (no athlete), and an ``owner`` —
    the coach whose library it belongs to. Templates are edited in the same
    designer grid as any plan; "new from template" / batch-deliver is a deep
    copy (``duplicate_for``).
    """

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        ACTIVE = "active", _("Active")
        ARCHIVED = "archived", _("Archived")

    relationship = models.ForeignKey(
        CoachAthlete,
        on_delete=models.CASCADE,
        related_name="plans",
        verbose_name=_("Relationship"),
        null=True,
        blank=True,
    )
    # Template plans (parity plan §3.4): a reusable program with no athlete.
    # ``owner`` is the coach whose template library it belongs to; regular
    # relationship plans leave it NULL (their coach rides the relationship).
    is_template = models.BooleanField(_("Template"), default=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="template_plans",
        verbose_name=_("Owner"),
        null=True,
        blank=True,
    )
    title = models.CharField(_("Title"), max_length=255)
    # Goals/focus are per-plan (D-b); the athlete's contraindications stay global.
    goal = models.CharField(_("Goal"), max_length=255, blank=True)
    status = models.CharField(
        _("Status"), max_length=16, choices=Status, default=Status.DRAFT
    )
    unit = models.CharField(
        _("Unit"), max_length=2, choices=Unit, default=Unit.KILOGRAMS
    )
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)

    objects = PlanQuerySet.as_manager()

    class Meta:
        ordering = ["-created"]
        verbose_name = "Plan"
        verbose_name_plural = "Plans"
        constraints = [
            # A template plan has no relationship — it belongs to its owner's
            # library, not to an athlete (parity plan §3.4).
            models.CheckConstraint(
                condition=(
                    models.Q(is_template=False) | models.Q(relationship__isnull=True)
                ),
                name="template_plan_has_no_relationship",
            ),
        ]

    def __str__(self):
        athlete = self.athlete
        if athlete is None:
            # Template plans (and any relationship-less row) have no athlete
            # to name — fall back to the bare title rather than crash.
            return f"{self.title} (template)" if self.is_template else self.title
        return f"{self.title} ({athlete.display_name()})"

    @property
    def coach(self):
        """The coach who owns this plan.

        Via the relationship for a regular plan; a template plan's coach is
        its ``owner``. May be ``None`` for a relationship-less row with no
        owner — callers beware.
        """
        if self.relationship_id is None:
            return self.owner
        return self.relationship.coach

    @property
    def athlete(self):
        """The plan's athlete."""
        if self.relationship_id is None:
            return None
        return self.relationship.athlete

    def is_editable_by(self, user):
        """Whether ``user`` (a coach) may open + edit this plan in the designer.

        Editable by its coach over an *active* relationship; a template plan
        is editable by its ``owner`` (no relationship required, §3.4). Mirrors
        ``PlanQuerySet.editable_by`` for a single fetched plan.
        """
        if self.is_template:
            return self.owner_id == user.id
        return (
            self.relationship_id is not None
            and self.relationship.coach_id == user.id
            and self.relationship.is_active
        )

    def scaffold(self, *, days=2):
        """Seed a minimal-but-usable starter tree onto this (bare) plan.

        One block, the current week, and ``days`` fixed training-day slots each
        with a starter exercise-row slot and its week-1 cell — so the designer
        opens onto an editable, deliverable grid rather than an empty shell
        (there is no add-mesocycle / add-week UI yet, and a day needs a row to
        edit). Used by ``CoachAthlete.create_plan``. Returns ``self``.
        """
        mesocycle = Mesocycle.objects.create(
            plan=self, name="Block 1", order=0, week_count=4
        )
        week = Week.objects.create(
            mesocycle=mesocycle,
            index=1,
            phase="Accum",
            volume=70,
            intensity=65,
        )
        for day in range(1, days + 1):
            slot = SessionSlot.objects.create(
                mesocycle=mesocycle, day_number=day, name=f"Day {day}", order=day - 1
            )
            Session.objects.create(week=week, session_slot=slot)
            exercise_slot = ExerciseSlot.objects.create(
                session_slot=slot, name="New exercise", order=0
            )
            Prescription.objects.create(exercise_slot=exercise_slot, week=week)
        return self

    def duplicate_for(self, relationship, *, title=None, status=None):
        """Deep-copy this plan's live program tree onto another relationship.

        The batch-deliver primitive (parity plan §3.1): each recipient gets an
        **independent, live-editable copy** — no link back to the source, no
        shared rows, so editing one client's program never touches another's.
        Copies only the *live* tree (soft-deleted weeks/days/rows stay behind);
        every cell's whole line stack — text, ``skipped``, sub-lines — comes
        across verbatim. ``delivered_at`` resets — the copy is undelivered
        until a deliver stamps it. Returns the new ``Plan``.
        """
        copy = Plan.objects.create(
            relationship=relationship,
            title=title or self.title,
            goal=self.goal,
            status=status or Plan.Status.DRAFT,
            unit=self.unit,
        )
        for mesocycle in self.mesocycles.order_by("order"):
            meso_copy = Mesocycle.objects.create(
                plan=copy,
                name=mesocycle.name,
                order=mesocycle.order,
                week_count=mesocycle.week_count,
            )
            live_slots = list(
                mesocycle.session_slots.filter(deleted_at__isnull=True).order_by(
                    "order", "day_number"
                )
            )
            slot_map = {}
            for slot in live_slots:
                slot_map[slot.pk] = SessionSlot.objects.create(
                    mesocycle=meso_copy,
                    day_number=slot.day_number,
                    name=slot.name,
                    bias=slot.bias,
                    order=slot.order,
                )
            exercise_map = {}
            live_exercise_slots = list(
                ExerciseSlot.objects.filter(
                    session_slot__in=live_slots, deleted_at__isnull=True
                ).order_by("order")
            )
            for row in live_exercise_slots:
                exercise_map[row.pk] = ExerciseSlot.objects.create(
                    session_slot=slot_map[row.session_slot_id],
                    exercise=row.exercise,
                    name=row.name,
                    order=row.order,
                    tags=list(row.tags or []),
                    tempo=row.tempo,
                    rest=row.rest,
                    note=row.note,
                )
            live_weeks = list(
                mesocycle.weeks.filter(deleted_at__isnull=True).order_by("index")
            )
            week_map = {}
            for week in live_weeks:
                week_map[week.pk] = Week.objects.create(
                    mesocycle=meso_copy,
                    index=week.index,
                    phase=week.phase,
                    volume=week.volume,
                    intensity=week.intensity,
                    is_deload=week.is_deload,
                )
            Session.objects.bulk_create(
                [
                    Session(
                        week=week_map[session.week_id],
                        session_slot=slot_map[session.session_slot_id],
                    )
                    for session in Session.objects.filter(
                        week__in=live_weeks,
                        session_slot__in=live_slots,
                        deleted_at__isnull=True,
                    )
                ]
            )
            Prescription.objects.bulk_create(
                [
                    Prescription(
                        exercise_slot=exercise_map[cell.exercise_slot_id],
                        week=week_map[cell.week_id],
                        line=cell.line,
                        text=cell.text,
                        skipped=cell.skipped,
                    )
                    for cell in Prescription.objects.filter(
                        week__in=live_weeks,
                        exercise_slot__in=live_exercise_slots,
                    )
                ]
            )
        return copy


class Mesocycle(models.Model):
    """A training block within a plan — one bar in the macrocycle rail."""

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="mesocycles",
        verbose_name=_("Plan"),
    )
    name = models.CharField(_("Name"), max_length=255)
    order = models.PositiveIntegerField(_("Order"), default=0)
    # Planned length; Week rows are only materialized for blocks being designed.
    week_count = models.PositiveIntegerField(_("Week count"), default=4)

    class Meta:
        ordering = ["order"]
        verbose_name = "Mesocycle"
        verbose_name_plural = "Mesocycles"
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "order"], name="unique_mesocycle_order"
            ),
        ]

    def __str__(self):
        return self.name

    def append_week(self):
        """Materialize the next week in this block — a new column, not a clone.

        Since the P0 fixed-lineup cutover, ``SessionSlot``/``ExerciseSlot`` are
        block-level identity **shared** across every week, so a new week never
        deep-copies sessions/exercise rows — it just adds a ``Session`` instance
        per live slot and ``Prescription`` cells per live exercise slot, with the
        row's freeform *text* (the whole line stack — prescription line plus any
        sub-lines, Phase 2a) carried forward from the latest live week as a
        starting point the coach then tweaks. Each new cell starts clean —
        ``skipped`` (a one-week exception) is never carried forward. The new
        week is live and visible to the athlete at once (2d, edits are live):
        adding a future week is a pure grid append with no pointer to move.
        ``week_count`` grows to stay >= the highest materialized index so the
        periodization rail stays honest. Returns the new ``Week``.

        A genuinely degenerate block — no weeks AND no slots yet — seeds one
        starter day (mirroring ``Plan.scaffold``) so the result is immediately
        editable. That is *not* the same thing as "no source week": since
        ``SessionSlot``/``ExerciseSlot`` are block-level identity that
        survives a week's soft delete (P0 fixed-lineup cutover), a block can
        have live slots with every one of its weeks soft-deleted — reachable
        because ``week_delete`` only guards the *plan's* last live week, not
        the block's (docs/meso/remove-current-week-plan.md §4b). Seeding a
        fresh starter day on top of those surviving slots would leave the old
        live slots with no session/cell in any live week — an orphaned block
        next to a redundant "Day 1." So the starter-day seed is gated on the
        slots, not the source week: a block with live slots always reuses
        them, whether or not it currently has a source week to carry text
        from.

        The source is the latest **live** week (a soft-deleted week is not a
        template to build from), but the new week's ``index`` is one past the
        highest index over **all** weeks, deleted included — a soft-deleted
        week keeps its ``(mesocycle, index)`` row, so indexing off only the
        live weeks could collide with it under ``unique_week_index``.

        """
        source = self.weeks.filter(deleted_at__isnull=True).order_by("-index").first()
        max_index = self.weeks.aggregate(m=models.Max("index"))["m"] or 0
        next_index = max_index + 1
        week = Week.objects.create(
            mesocycle=self,
            index=next_index,
            phase=source.phase if source else "",
            volume=source.volume if source else 0,
            intensity=source.intensity if source else 0,
            is_deload=source.is_deload if source else False,
        )
        live_slots = list(
            self.session_slots.filter(deleted_at__isnull=True).order_by(
                "order", "day_number"
            )
        )
        if source is None and not live_slots:
            slot = SessionSlot.objects.create(
                mesocycle=self, day_number=1, name="Day 1", order=0
            )
            Session.objects.create(week=week, session_slot=slot)
            exercise_slot = ExerciseSlot.objects.create(
                session_slot=slot, name="New exercise", order=0
            )
            Prescription.objects.create(exercise_slot=exercise_slot, week=week)
        else:
            Session.objects.bulk_create(
                [Session(week=week, session_slot=slot) for slot in live_slots]
            )
            # The whole line stack carries forward (Phase 2a): line 0's
            # prescription text plus any sub-lines (the RPE row, cues) — the
            # coach then tweaks the new column. ``skipped`` (a one-week
            # exception) never carries. No source week (the emptied-but-slots-
            # survive edge case above) means there's nothing to carry forward
            # at all — ``source_cells`` just stays empty and every cell below
            # falls through to its blank default.
            source_cells = defaultdict(dict)
            if source is not None:
                for cell in Prescription.objects.filter(
                    week=source, exercise_slot__deleted_at__isnull=True
                ):
                    source_cells[cell.exercise_slot_id][cell.line] = cell.text
            live_exercise_slots = ExerciseSlot.objects.filter(
                session_slot__in=live_slots, deleted_at__isnull=True
            )
            new_cells = []
            for exercise_slot in live_exercise_slots:
                lines = source_cells.get(exercise_slot.pk) or {0: ""}
                for line, text in lines.items():
                    new_cells.append(
                        Prescription(
                            exercise_slot=exercise_slot,
                            week=week,
                            line=line,
                            text=text,
                        )
                    )
            if new_cells:
                Prescription.objects.bulk_create(new_cells)
        if week.index > self.week_count:
            self.week_count = week.index
            self.save(update_fields=["week_count"])
        return week


class SessionSlot(models.Model):
    """The fixed DAY definition, shared across every week of a mesocycle.

    The P0 fixed-lineup cutover: a training day's identity (name/bias/order)
    used to live per-week on ``Session`` and get deep-copied on every
    ``append_week``; it now lives once here, at the block level. A ``Session``
    is just this slot's per-week instance (a thin join row anchoring logging).
    """

    mesocycle = models.ForeignKey(
        Mesocycle,
        on_delete=models.CASCADE,
        related_name="session_slots",
        verbose_name=_("Mesocycle"),
    )
    day_number = models.PositiveIntegerField(_("Day number"))
    name = models.CharField(_("Name"), max_length=255, blank=True)
    bias = models.CharField(_("Bias"), max_length=255, blank=True)
    order = models.PositiveIntegerField(_("Order"), default=0)
    # Soft delete (designer framework Phase 0) — see ``Week.deleted_at``. Because
    # this row is now the day's *only* identity (shared across weeks), deleting
    # it removes the day from the whole block at once — see ``soft_delete``.
    deleted_at = models.DateTimeField(
        _("Deleted at"), null=True, blank=True, default=None
    )

    class Meta:
        ordering = ["order", "day_number"]
        verbose_name = "Session slot"
        verbose_name_plural = "Session slots"

    def __str__(self):
        return f"Day {self.day_number} · {self.name}".rstrip(" ·")

    def soft_delete(self):
        """Remove this day from the whole block: stamp self + cascade.

        A ``SessionSlot`` is block-wide identity, so deleting it is a
        block-wide removal (the new fixed-lineup semantics — this is not the
        same as the old per-week ``Session`` delete). Cascades to this slot's
        live ``ExerciseSlot`` rows and every week's ``Session`` instance of this
        day, mirroring ``Week.soft_delete``'s convention. Returns ``self``.
        """
        now = timezone.now()
        self.deleted_at = now
        self.save(update_fields=["deleted_at"])
        self.exercise_slots.filter(deleted_at__isnull=True).update(deleted_at=now)
        self.sessions.filter(deleted_at__isnull=True).update(deleted_at=now)
        return self


class ExerciseSlot(models.Model):
    """The fixed EXERCISE row, shared across every week of a mesocycle.

    A table row's identity — catalog link/free-text name/tags — used to live
    per-week on ``ExercisePrescription`` and get deep-copied on every
    ``append_week``; it now lives once here. The per-week numbers (and rare
    one-week exceptions) live on the ``Prescription`` cell instead.
    """

    session_slot = models.ForeignKey(
        SessionSlot,
        on_delete=models.CASCADE,
        related_name="exercise_slots",
        verbose_name=_("Session slot"),
    )
    exercise = models.ForeignKey(
        "exercises.Exercise",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meso_exercise_slots",
        verbose_name=_("Catalog exercise"),
    )
    name = models.CharField(_("Name"), max_length=255)
    order = models.PositiveIntegerField(_("Order"), default=0)
    # Tags describe identity, so they live here (moved off the old
    # per-week ``ExercisePrescription.tags``).
    tags = models.JSONField(_("Tags"), default=list, blank=True)
    # Spreadsheet-parity per-EXERCISE columns (Phase 2a, plan §2.2 / D2):
    # Tempo (``201``/``ISO``/``DYN``) and Rest (``60s``/``2-3m``/``PRN``) are
    # per-row in every template generation, not per-week — they moved here off
    # the old per-week cell. ``note`` is the per-exercise instructions/cues
    # column (the templates' merged Coach Comment — "where the how-to lives").
    tempo = models.CharField(_("Tempo"), max_length=64, blank=True)
    rest = models.CharField(_("Rest"), max_length=64, blank=True)
    note = models.TextField(_("Instructions"), blank=True)
    # Soft delete (designer framework Phase 0) — see ``Week.deleted_at``. Removes
    # this exercise row from the whole block at once (all weeks).
    deleted_at = models.DateTimeField(
        _("Deleted at"), null=True, blank=True, default=None
    )

    class Meta:
        ordering = ["order"]
        verbose_name = "Exercise slot"
        verbose_name_plural = "Exercise slots"

    def __str__(self):
        return self.name

    @property
    def is_catalog_linked(self):
        """True when this row is backed by a catalog ``Exercise`` (B4 hybrid)."""
        return self.exercise_id is not None

    def soft_delete(self):
        """Remove this exercise row from the whole block (all weeks at once).

        No cascade needed: a ``Prescription`` cell has no ``deleted_at`` of its
        own — it is live only while both its slot and its week are live, so
        hiding this slot hides every week's cell for it via the join. Returns
        ``self``.
        """
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])
        return self


class Week(models.Model):
    """One week within a mesocycle — a column in the designer's week strip."""

    mesocycle = models.ForeignKey(
        Mesocycle,
        on_delete=models.CASCADE,
        related_name="weeks",
        verbose_name=_("Mesocycle"),
    )
    index = models.PositiveIntegerField(_("Week number"))
    phase = models.CharField(_("Phase"), max_length=64, blank=True)
    volume = models.PositiveIntegerField(_("Volume"), default=0)
    intensity = models.PositiveIntegerField(_("Intensity"), default=0)
    is_deload = models.BooleanField(_("Deload"), default=False)
    # When the coach last sent the deliver nudge (2d: a notify marker +
    # snapshot timestamp, never a visibility gate — the athlete sees live weeks
    # regardless).
    delivered_at = models.DateTimeField(_("Delivered at"), null=True, blank=True)
    # Soft delete (designer framework Phase 0): a week is *live* iff this is
    # None. The delete endpoint stamps only this row — children are hidden by
    # serializers/lookups filtering live rows at each level of the walk, not by
    # a cascading write, so an independently-deleted child stays deleted if a
    # later undo restores this week. See docs/archive/meso/designer-framework-plan.md.
    deleted_at = models.DateTimeField(
        _("Deleted at"), null=True, blank=True, default=None
    )

    class Meta:
        ordering = ["index"]
        verbose_name = "Week"
        verbose_name_plural = "Weeks"
        constraints = [
            models.UniqueConstraint(
                fields=["mesocycle", "index"], name="unique_week_index"
            ),
        ]

    def __str__(self):
        return f"{self.mesocycle.name} · Wk {self.index}"

    def soft_delete(self):
        """Stamp this week deleted, cascading to its ``Session`` instances.

        A ``Session`` is now a real (week, ``SessionSlot``) join row, so — per
        the P0 fixed-lineup cascade rules — soft-deleting a week also stamps its
        sessions (its ``Prescription`` cells carry no ``deleted_at`` of their own
        and are hidden via the join to this dead week regardless). Returns
        ``self``.
        """
        now = timezone.now()
        self.deleted_at = now
        self.save(update_fields=["deleted_at"])
        self.sessions.filter(deleted_at__isnull=True).update(deleted_at=now)
        return self


class Session(models.Model):
    """A training day *within a week* — a column in the designer grid.

    THINNED by the P0 fixed-lineup cutover: this is now just a (week ×
    ``SessionSlot``) instance, anchoring logging (``SessionLog``). Identity
    (day_number/name/bias/order) delegates to the slot via properties so
    existing read sites (``session.name`` etc.) keep working unchanged.
    """

    week = models.ForeignKey(
        Week,
        on_delete=models.CASCADE,
        related_name="sessions",
        verbose_name=_("Week"),
    )
    session_slot = models.ForeignKey(
        SessionSlot,
        on_delete=models.CASCADE,
        related_name="sessions",
        verbose_name=_("Session slot"),
    )
    # Soft delete (designer framework Phase 0) — see ``Week.deleted_at``.
    deleted_at = models.DateTimeField(
        _("Deleted at"), null=True, blank=True, default=None
    )

    class Meta:
        ordering = ["session_slot__order", "session_slot__day_number"]
        verbose_name = "Session"
        verbose_name_plural = "Sessions"
        constraints = [
            models.UniqueConstraint(
                fields=["week", "session_slot"], name="unique_session_week_slot"
            ),
        ]

    def __str__(self):
        return f"Day {self.day_number} · {self.name}".rstrip(" ·")

    @property
    def day_number(self):
        return self.session_slot.day_number

    @property
    def name(self):
        return self.session_slot.name

    @property
    def bias(self):
        return self.session_slot.bias

    @property
    def order(self):
        return self.session_slot.order

    def cells(self):
        """This week's live line-0 (prescription) cells for this day, in row order.

        Replaces the old ``session.prescriptions`` related manager: a cell
        lives iff its ``ExerciseSlot`` is live (the week is already pinned by
        ``self.week``, which is assumed live — callers already filter weeks).
        Line 0 only — one cell per exercise row, which is what every "a row's
        cell this week" caller (logging, snapshots, reorder id-sets) means;
        the freeform sub-lines (Phase 2a) come from ``line_cells``.
        """
        return (
            Prescription.objects.filter(
                week=self.week,
                exercise_slot__session_slot=self.session_slot,
                exercise_slot__deleted_at__isnull=True,
                line=0,
            )
            .select_related("exercise_slot")
            .order_by("exercise_slot__order")
        )

    def line_cells(self):
        """This week's live sub-line cells (line >= 1) for this day, stack order.

        The freeform per-week sub-rows beneath each prescription (Phase 2a,
        plan §2.3/§2.6): RPE rows, cues, logged deviations. Blank-text rows
        are kept (a cleared sub-line is a blank cell, not a deleted row) —
        serializers drop them at render time.
        """
        return (
            Prescription.objects.filter(
                week=self.week,
                exercise_slot__session_slot=self.session_slot,
                exercise_slot__deleted_at__isnull=True,
                line__gte=1,
            )
            .select_related("exercise_slot")
            .order_by("exercise_slot__order", "line")
        )

    def trainable_cells(self):
        """This week's cells the athlete actually trains — excludes one-week skips.

        The week-at-a-time display/logging surfaces (the designer program grid, the
        athlete session + logger, results) render only trainable rows: a ``skipped``
        cell is "not trained this week" and must not appear as a loggable blank row
        (the P1 multi-week table renders it as an em-dash instead). ``cells()`` still
        returns every cell for structure-preserving logic (snapshots).
        """
        return self.cells().filter(skipped=False)


class Prescription(models.Model):
    """A CELL = one ``ExerciseSlot`` row × one ``Week`` × one ``line`` of text.

    The spreadsheet-parity cutover (Phase 2a, docs/meso/spreadsheet-parity-plan.md
    §2): a cell stops being seven structured fields and becomes ONE freeform
    ``text`` string (``4 x 6, RPE 9, 225`` / ``3 x 12-15`` / ``AMRAP`` — whatever
    the coach types). Structure is *derived on demand* by
    ``parsing.parse_prescription`` — never persisted as truth.

    ``line`` orders a vertical stack of per-week cells within an exercise row:
    line 0 is the sets/reps prescription, lines 1+ are optional freeform
    sub-rows (the templates' per-week RPE row, logged execution, an in-cell
    substitution, a note — §2.3/§2.6; nothing is hardcoded to "RPE"). Each
    (line × week) cell is independently filled or empty; the old structured
    ``swap_*``/per-week ``note`` collapse into these sub-lines as plain text.

    ``skipped`` survives as the one structured per-week exception (§2.1): the
    em-dash "not trained this week" cell, which athlete surfaces must exclude
    from logging — distinct from the row not existing at all.

    There is deliberately **no** ``deleted_at`` here: a cell lives iff its slot
    *and* its week are both live; clearing a sub-line = blanking its ``text``
    (spreadsheet semantics), not deleting the row — keeps undo simple.

    Identity is entirely the slot's now (the one-week ``swap_*`` override is
    gone — a substitution is text in a sub-line): the resolving properties
    below just delegate, keeping read sites (``one_rm`` keying,
    ``_exercise_key``, …) working unchanged.
    """

    exercise_slot = models.ForeignKey(
        ExerciseSlot,
        on_delete=models.CASCADE,
        related_name="cells",
        verbose_name=_("Exercise slot"),
    )
    week = models.ForeignKey(
        Week,
        on_delete=models.CASCADE,
        related_name="cells",
        verbose_name=_("Week"),
    )
    # Position in the exercise's vertical stack: 0 = the prescription line,
    # 1+ = freeform sub-rows (RPE, cues, logged deviations, …).
    line = models.PositiveIntegerField(_("Line"), default=0)
    text = models.TextField(_("Text"), blank=True)
    # Per-week exception: this week's cell is a deliberate skip (renders as an
    # em-dash), distinct from the row not existing at all. Applies to the
    # exercise × week, so it is only ever meaningful on line 0.
    skipped = models.BooleanField(_("Skipped"), default=False)
    # The athlete authored this cell's current text via their own tracking
    # surface (Phase 4a). It keeps the cell OUT of the coach's undo/redo
    # snapshot machinery: an athlete's freeform sub-line write records no
    # ``PlanAction``, and a coach undo/redo must never overwrite or hard-delete
    # it (``history.py``). A coach edit to the same cell reclaims it (flips this
    # back to ``False``), folding it into coach history again. Existing cells
    # are coach-authored, so the default is ``False`` and no backfill is needed.
    athlete_authored = models.BooleanField(_("Athlete authored"), default=False)

    class Meta:
        ordering = ["exercise_slot__order", "line"]
        verbose_name = "Prescription"
        verbose_name_plural = "Prescriptions"
        constraints = [
            models.UniqueConstraint(
                fields=["exercise_slot", "week", "line"],
                name="unique_cell_slot_week_line",
            ),
        ]

    def __str__(self):
        return self.name

    def parsed(self):
        """Best-effort derived structure for this cell's text (never raises)."""
        from .parsing import parse_prescription

        return parse_prescription(self.text)

    @property
    def name(self):
        """The row's name — always the slot's (identity is never per-week now)."""
        return self.exercise_slot.name

    @property
    def exercise(self):
        return self.exercise_slot.exercise

    @property
    def exercise_id(self):
        return self.exercise_slot.exercise_id

    @property
    def tags(self):
        """Identity tags always come from the slot — never per-week."""
        return list(self.exercise_slot.tags or [])

    @property
    def is_catalog_linked(self):
        """True when this row is backed by a catalog ``Exercise`` (B4)."""
        return self.exercise_id is not None


# ---------------------------------------------------------------------------
# Logging (models now, athlete-facing UI in a later slice)
#
# Defined here so results/`last`-load derivation has a home; the logging UI and
# PWA land with the athlete slice.
# ---------------------------------------------------------------------------


class SessionLog(models.Model):
    """An athlete's record of having trained a planned session."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        DONE = "done", _("Done")

    session = models.ForeignKey(
        Session,
        on_delete=models.CASCADE,
        related_name="logs",
        verbose_name=_("Session"),
    )
    athlete = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="meso_session_logs",
        verbose_name=_("Athlete"),
    )
    date = models.DateField(_("Date"), null=True, blank=True)
    status = models.CharField(
        _("Status"), max_length=16, choices=Status, default=Status.PENDING
    )
    notes = models.TextField(_("Notes"), blank=True)
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Session log"
        verbose_name_plural = "Session logs"

    def __str__(self):
        return f"{self.athlete.display_name()} · {self.session}"


# ---------------------------------------------------------------------------
# Delivery / lightweight versioning (Phase 4; reframed by 2d)
#
# Delivering a week stamps ``Week.delivered_at`` and records a ``WeekDelivery``
# snapshot of the week at that moment. Since 2d (parity plan §3.3) delivery is
# a one-time notify + history record — never a visibility gate (the athlete
# sees every edit live). The snapshots are retention/history: they feed the
# deliver screen's optional "changes since last delivery" diff and, later, the
# PR engine's prescribed-vs-performed record.
# ---------------------------------------------------------------------------


class WeekDelivery(models.Model):
    """A snapshot of a week at the moment a coach delivered it to the athlete."""

    week = models.ForeignKey(
        Week,
        on_delete=models.CASCADE,
        related_name="deliveries",
        verbose_name=_("Week"),
    )
    delivered_at = models.DateTimeField(_("Delivered at"))
    payload = models.JSONField(_("Payload"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)

    class Meta:
        ordering = ["-delivered_at"]
        verbose_name = "Week delivery"
        verbose_name_plural = "Week deliveries"

    def __str__(self):
        return f"{self.week} · delivered {self.delivered_at:%Y-%m-%d}"


# ---------------------------------------------------------------------------
# Agent proposals (Phase 1 of the agent slice — B6)
#
# The agent is a *proposal engine behind the existing review gate*: it writes
# ``ProposedChange`` rows grouped into an ``AgentProposalBatch``; the coach still
# approves (apply lands in Phase 2). Proposals are inert until then, so writing
# them is safe. See ``docs/archive/meso/agent-plan.md``.
# ---------------------------------------------------------------------------


class AgentProposalBatch(models.Model):
    """One agent run: the coach's instruction + the batch of edits it proposed.

    The batch is also the **per-run usage ledger** (agent-usage tracking v1): one
    Claude call today, so the token usage + estimated cost live right on the row
    (``docs/meso/agent-usage-plan.md`` U1). ``coach`` (who pays) + ``plan`` (→
    athlete) + ``model`` were already the attribution; the usage/cost
    columns and the ``trigger`` / ``billing_status`` snapshots close the gap so a
    later report can split COGS (paid) vs CAC (free/trial) and find the heavy seats.
    """

    class Status(models.TextChoices):
        # The agent run happens off the request thread (Phase 4): a batch starts
        # DRAFTING, then the background job flips it to PENDING (ready for review)
        # or FAILED (with the reason in ``error``).
        DRAFTING = "drafting", _("Drafting")
        PENDING = "pending", _("Pending review")
        FAILED = "failed", _("Failed")
        APPLIED = "applied", _("Applied")
        DISMISSED = "dismissed", _("Dismissed")

    class Trigger(models.TextChoices):
        # What kicked off the run — a slicing dimension for the usage report.
        # ``eval`` runs (the golden corpus) are excluded from cost reporting.
        MANUAL = "manual", _("Manual")
        DRAFT = "draft", _("Draft with AI")
        EVAL = "eval", _("Eval")

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="proposal_batches",
        verbose_name=_("Plan"),
    )
    # The block the coach was viewing when they kicked off this run (§4b,
    # docs/meso/remove-current-week-plan.md) — captured once at request time
    # and frozen here, because grounding + validation run in a BACKGROUND JOB
    # and apply happens on a LATER coach request; nothing downstream can re-read
    # a live "current" pointer across that time gap without risking a different
    # answer each time (or, post-``is_current``, silently defaulting to block 1
    # while the coach works block 2 — the exact regression this field fixes).
    # ``SET_NULL`` IS LOAD-BEARING: this batch is also the usage/cost ledger
    # (agent-usage tracking v1), so deleting a block must never cascade into
    # deleting billing history. ``null=True`` additionally covers legacy rows
    # (created before this field existed) and any caller that doesn't pin a
    # block (the eval harness, direct/test callers) — those degrade to the
    # empty-block grounding guard, never to a silent earliest-live re-derivation.
    mesocycle = models.ForeignKey(
        "Mesocycle",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposal_batches",
        verbose_name=_("Block"),
    )
    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="meso_proposal_batches",
        verbose_name=_("Coach"),
    )
    instruction = models.TextField(_("Instruction"))
    summary = models.TextField(_("Agent summary"), blank=True)
    # The Claude model id the batch was produced with (eval/debugging).
    model = models.CharField(_("Model"), max_length=64, blank=True)
    status = models.CharField(
        _("Status"), max_length=16, choices=Status, default=Status.PENDING
    )
    # Why a background run failed — surfaced to the coach via the status poll.
    error = models.TextField(_("Error"), blank=True)
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)

    # -- agent-usage tracking (per-run cost attribution; v1) -------------
    # Token usage captured from the Claude call (``agent.client.RunUsage``). Zero
    # for a run that made no API call (a scripted/test client) or one that failed
    # before the SDK returned. Cache writes ≈ 1.25× input, cache reads ≈ 0.1×.
    input_tokens = models.PositiveIntegerField(_("Input tokens"), default=0)
    output_tokens = models.PositiveIntegerField(_("Output tokens"), default=0)
    cache_creation_input_tokens = models.PositiveIntegerField(
        _("Cache-write input tokens"), default=0
    )
    cache_read_input_tokens = models.PositiveIntegerField(
        _("Cache-read input tokens"), default=0
    )
    # Completed Claude calls in this run — 0 until one returns (a scripted/failed
    # run never overcounts), 1 today on success; >1 once multi-turn lands.
    api_calls = models.PositiveIntegerField(_("API calls"), default=0)
    # Anthropic ``_request_id`` (tracing / support escalation) + the stop reason
    # (``max_tokens`` truncation, ``refusal``, …) for diagnostics.
    request_id = models.CharField(_("Anthropic request id"), max_length=128, blank=True)
    stop_reason = models.CharField(_("Stop reason"), max_length=32, blank=True)
    # Wall-clock latency of the Claude call (null until measured / on early failure).
    duration_ms = models.PositiveIntegerField(_("Duration (ms)"), null=True, blank=True)
    # tokens × per-model rate, computed at write time so a later price change can't
    # rewrite history. An internal *estimate* — the Anthropic invoice is the truth.
    # Null when the model isn't in the rate table (don't guess; the report flags it).
    estimated_cost_usd = models.DecimalField(
        _("Estimated cost (USD)"),
        max_digits=10,
        decimal_places=6,
        null=True,
        blank=True,
    )
    # Slicing dimensions snapshotted at run time (lossy to reconstruct later).
    trigger = models.CharField(
        _("Trigger"), max_length=16, choices=Trigger.choices, default=Trigger.MANUAL
    )
    # The coach's billing tier when the run fired (free/trialing/active/comped/…) —
    # the COGS-vs-CAC split for the usage report. Blank for legacy rows.
    billing_status = models.CharField(
        _("Billing status at run time"), max_length=16, blank=True
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Agent proposal batch"
        verbose_name_plural = "Agent proposal batches"

    def __str__(self):
        return f"Proposal for {self.plan.title} ({self.changes.count()} changes)"


class ProposedChange(models.Model):
    """A single program edit the agent proposes, awaiting coach approval.

    The structured targets (``session``/``prescription``) are validated to belong
    to the batch's plan before persistence (``agent.validation``). The display
    fields mirror the prototype's review badges; ``payload`` is reserved for the
    apply step (Phase 2).
    """

    class Kind(models.TextChoices):
        SWAP = "swap", _("Swap")
        PROGRESS = "progress", _("Progress")
        VOLUME = "volume", _("Volume")
        DELOAD = "deload", _("Deload")
        # Introduce a NEW exercise row into a session (no row to edit) — the verb
        # that lets the agent draft a program onto a bare scaffold.
        ADD = "add", _("Add")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    batch = models.ForeignKey(
        AgentProposalBatch,
        on_delete=models.CASCADE,
        related_name="changes",
        verbose_name=_("Batch"),
    )
    kind = models.CharField(_("Kind"), max_length=16, choices=Kind)
    session = models.ForeignKey(
        Session,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposed_changes",
        verbose_name=_("Session"),
    )
    prescription = models.ForeignKey(
        Prescription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposed_changes",
        verbose_name=_("Prescription"),
    )
    day_label = models.CharField(_("Day label"), max_length=128, blank=True)
    title = models.CharField(_("Title"), max_length=255)
    before = models.CharField(_("Before"), max_length=255, blank=True)
    after = models.CharField(_("After"), max_length=255, blank=True)
    rationale = models.TextField(_("Rationale"), blank=True)
    honors = models.CharField(_("Honors"), max_length=255, blank=True)
    # The exercise a swap introduces — what the contraindication backstop checks.
    introduces_exercise = models.CharField(
        _("Introduces exercise"), max_length=255, blank=True
    )
    # Reserved for the apply step (Phase 2): the structured edit to perform.
    payload = models.JSONField(_("Payload"), default=dict, blank=True)
    status = models.CharField(
        _("Status"), max_length=16, choices=Status, default=Status.PENDING
    )
    order = models.PositiveIntegerField(_("Order"), default=0)
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)

    class Meta:
        ordering = ["order", "created_at"]
        verbose_name = "Proposed change"
        verbose_name_plural = "Proposed changes"

    def __str__(self):
        return self.title


class LoggedSet(models.Model):
    """A single set the athlete logged against a prescription."""

    session_log = models.ForeignKey(
        SessionLog,
        on_delete=models.CASCADE,
        related_name="sets",
        verbose_name=_("Session log"),
    )
    prescription = models.ForeignKey(
        Prescription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logged_sets",
        verbose_name=_("Prescription"),
    )
    set_number = models.PositiveIntegerField(_("Set number"), default=1)
    reps = models.CharField(_("Reps"), max_length=32, blank=True)
    load = models.CharField(_("Load"), max_length=32, blank=True)
    rpe = models.CharField(_("RPE"), max_length=32, blank=True)
    # Parse-at-commit (5a, docs/meso/parse-at-commit-plan.md §4). Points at the
    # athlete-authored sub-line cell (line >= 1) whose freeform text
    # ``parse_performed`` classified into this set. NULL = a structured-logger
    # origin (``athlete_log_session``). Triple duty: discriminator (the
    # structured logger's delete scopes around parsed rows), de-dup link
    # (presenters suppress one display channel), and idempotency key
    # (``(session_log, source_line)`` — a re-blur deletes-then-recreates rather
    # than appending). SET_NULL on a hard-deleted sub-line intentionally
    # orphans the set as structured-origin-like (it survives, ``prescription``
    # still points at line-0).
    source_line = models.ForeignKey(
        Prescription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="parsed_sets",
        verbose_name=_("Source line"),
    )

    class Meta:
        ordering = ["set_number"]
        verbose_name = "Logged set"
        verbose_name_plural = "Logged sets"

    def __str__(self):
        return f"Set {self.set_number}"


# ---------------------------------------------------------------------------
# Web push subscriptions (athlete PWA — Phase 4b, decision S3/S7)
#
# A browser ``PushSubscription`` an athlete registered so the server can push
# delivery notifications. The ``endpoint`` is the push service's per-device URL
# (unique); ``p256dh``/``auth`` are the client keys the payload is encrypted to.
# An athlete may have several (one per device/browser). Dead endpoints (the push
# service answers 404/410) are pruned on send.
# ---------------------------------------------------------------------------


class PushSubscription(models.Model):
    """A device's web-push subscription owned by an athlete."""

    athlete = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="meso_push_subscriptions",
        verbose_name=_("Athlete"),
    )
    endpoint = models.CharField(_("Endpoint"), max_length=512, unique=True)
    p256dh = models.CharField(_("P256DH key"), max_length=255)
    auth = models.CharField(_("Auth secret"), max_length=255)
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Push subscription"
        verbose_name_plural = "Push subscriptions"

    def __str__(self):
        return f"{self.athlete.display_name()} · {self.endpoint[:32]}…"

    def as_subscription_info(self):
        """The ``subscription_info`` dict pywebpush expects."""
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }


# ---------------------------------------------------------------------------
# Persisted estimated 1RM (units & RPE/%1RM slice, S2 — the deferred follow-up)
#
# A %1RM target ("75%") is an *intensity*, not a weight; turning it into a bar
# load needs the athlete's 1RM. Phase 2b let the athlete enter that estimate, but
# it lived only in the browser's localStorage — per-device, invisible to the
# coach. ``AthleteOneRm`` promotes it to a real row, **auto-derived from the
# athlete's logged history** (the best Epley estimate per lift), so it survives a
# device change, powers the logger's suggested load on any device, and is visible
# to the coach in the designer when they prescribe a %1RM. See
# ``one_rm.py`` (derive/refresh/read) and ``docs/archive/meso/one-rm-plan.md``.
# ---------------------------------------------------------------------------


class AthleteOneRm(models.Model):
    """An athlete's estimated one-rep max for a lift, derived from their logs.

    Keyed by lift *identity*, the hybrid B4 rule mirroring
    ``serializers._exercise_key``: a catalog-linked lift by its ``Exercise`` FK, a
    free-text lift by its normalized name. The denormalized ``key`` carries that
    identity (``"id:<pk>"`` / ``"name:<lower>"``) so a single ``unique(athlete,
    key)`` constraint holds whether or not the lift is catalog-backed. ``value``
    is the estimate in ``unit`` (the unit the logged work was recorded in); a
    %1RM target scaled against it is plate-rounded client-side.

    ``source`` records whether the estimate is **auto-derived from the athlete's
    logs** (the default) or **manually entered** by the athlete (Phase 2). A
    manual value is the athlete's own number: ``refresh_one_rms`` never clobbers
    it (logs only ever raised the derived estimate), and it survives a device
    change and is visible to the coach — the gap the per-device localStorage
    override left open. Clearing a manual value reverts the lift to its
    log-derived estimate. See ``one_rm.py`` and ``docs/archive/meso/one-rm-plan.md``.
    """

    class Source(models.TextChoices):
        LOGGED = "logged", _("Auto-derived from logs")
        MANUAL = "manual", _("Athlete-entered")

    athlete = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="meso_one_rms",
        verbose_name=_("Athlete"),
    )
    # Catalog link when the lift is catalog-backed (B4 hybrid); the same lift's
    # ``key`` then matches by FK across prescriptions. Free-text lifts match by
    # name and leave this null.
    exercise = models.ForeignKey(
        "exercises.Exercise",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meso_one_rms",
        verbose_name=_("Catalog exercise"),
    )
    name = models.CharField(_("Lift name"), max_length=255)
    key = models.CharField(_("Lift key"), max_length=300, editable=False)
    value = models.DecimalField(_("Estimated 1RM"), max_digits=7, decimal_places=2)
    unit = models.CharField(
        _("Unit"), max_length=2, choices=Unit, default=Unit.KILOGRAMS
    )
    source = models.CharField(
        _("Source"), max_length=10, choices=Source, default=Source.LOGGED
    )
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Time last modified"), auto_now=True)

    class Meta:
        ordering = ["athlete_id", "name"]
        verbose_name = "Athlete 1RM"
        verbose_name_plural = "Athlete 1RMs"
        constraints = [
            models.UniqueConstraint(
                fields=["athlete", "key"], name="unique_athlete_one_rm"
            ),
        ]

    def __str__(self):
        return f"{self.athlete.display_name()} · {self.name}: {self.value}"

    def save(self, *args, **kwargs):
        # ``key`` is derived, never hand-set: keep it authoritative on every write
        # (admin/factory/tests included) so the ``unique(athlete, key)`` constraint
        # holds and a blank key can never collide every lift for an athlete.
        from .one_rm import key_str

        self.key = key_str(self.exercise_id, self.name)
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Undo/redo op-log (designer framework Phase 1)
#
# The designer needs plan-wide undo/redo, built on Phase 0's soft delete: every
# mutating designer endpoint records ONE ``PlanAction`` on the undo stack — a
# short human ``label`` plus a plan-wide ``snapshot`` of the editable state
# taken just BEFORE the mutation (``history.serialize_plan_snapshot``). Undo
# pops the max-seq undo row, restores its snapshot, and pushes the mirror-image
# redo row (same seq+label, snapshot = the state just left); redo is the exact
# mirror. See ``history.py`` (the snapshot serializer/restorer + the
# ``record_plan_action`` recorder) and ``docs/archive/meso/designer-framework-plan.md``.
# ---------------------------------------------------------------------------


class PlanAction(models.Model):
    """One entry in a plan's undo/redo op-log.

    ``stack`` + ``seq`` together give every action a stable slot: undo pops the
    max-``seq`` ``undo`` row and pushes a ``redo`` row at the *same* seq (redo
    mirrors it back); a fresh mutation always allocates a new, higher seq and
    clears whatever redo rows existed (a fork in history drops the abandoned
    future). ``snapshot`` is plan-wide — every ``Week``/``SessionSlot``/
    ``ExerciseSlot``/``Session``/``Prescription`` row
    belonging to the plan, including soft-deleted ones, so an undo can
    resurrect a delete or retract an add without ever hard-deleting or
    recreating a row.
    """

    class Stack(models.TextChoices):
        UNDO = "undo", _("Undo")
        REDO = "redo", _("Redo")

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="actions",
        verbose_name=_("Plan"),
    )
    stack = models.CharField(
        _("Stack"), max_length=8, choices=Stack, default=Stack.UNDO
    )
    seq = models.PositiveIntegerField(_("Sequence"))
    label = models.CharField(_("Label"), max_length=80)
    snapshot = models.JSONField(_("Snapshot"))
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)

    class Meta:
        ordering = ["seq"]
        verbose_name = "Plan action"
        verbose_name_plural = "Plan actions"
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "stack", "seq"], name="unique_plan_action_seq"
            ),
        ]

    def __str__(self):
        return f"{self.plan_id} · {self.stack} #{self.seq} · {self.label}"
