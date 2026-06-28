"""Tenancy, roles, and the coach↔athlete relationship spine for Meso.

This is the first real (DB-backed) slice of the Meso program designer, which
once ran entirely on a fixtures module (since retired now every screen is
DB-backed). See ``docs/meso/persistence-plan.md`` (Phase 1) and
``docs/meso/decisions.md``.

Multi-coach SaaS (B1): every coach is an account, athletes are Users who log in
(B2), and the link between them is a many-to-many, athlete-consented
relationship (N1). Roles are marked by the presence of a ``CoachProfile`` /
``AthleteProfile`` rather than flags on the User model (N3).
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
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
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)

    class Meta:
        verbose_name = "Coach profile"
        verbose_name_plural = "Coach profiles"

    def __str__(self):
        return self.display_name or self.user.display_name()


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

    def pending(self):
        return self.filter(status__in=CoachAthlete.PENDING_STATUSES)

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
            models.CheckConstraint(
                condition=~models.Q(coach=models.F("athlete")),
                name="coach_athlete_distinct",
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

    def recipient(self):
        """The party whose acceptance the link is waiting on (None if not pending)."""
        if self.status == self.Status.PENDING_COACH_INVITE:
            return self.athlete
        if self.status == self.Status.PENDING_ATHLETE_REQUEST:
            return self.coach
        return None


# ---------------------------------------------------------------------------
# Program schema (Phase 2)
#
# The periodized plan a coach builds for an athlete:
#
#   Plan → Mesocycle → Week → Session → ExercisePrescription
#
# Owned per coach↔athlete relationship (D-a) so each coach programs
# independently. ``ExercisePrescription`` links to the catalog ``Exercise`` when
# one matches and falls back to free text otherwise (the B4 hybrid). The shape is
# driven by what the designer (``static/js/meso.js``) renders; see
# ``serializers.serialize_plan`` for the mapping back to that shape.
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

    def active(self):
        return self.filter(status=Plan.Status.ACTIVE)


class Plan(models.Model):
    """A periodized training plan, owned by one coach↔athlete relationship (D-a)."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        ACTIVE = "active", _("Active")
        ARCHIVED = "archived", _("Archived")

    relationship = models.ForeignKey(
        CoachAthlete,
        on_delete=models.CASCADE,
        related_name="plans",
        verbose_name=_("Relationship"),
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

    def __str__(self):
        return f"{self.title} ({self.athlete.display_name()})"

    @property
    def coach(self):
        return self.relationship.coach

    @property
    def athlete(self):
        return self.relationship.athlete


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
    is_current = models.BooleanField(_("Current"), default=False)
    delivered_at = models.DateTimeField(_("Delivered at"), null=True, blank=True)

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


class Session(models.Model):
    """A training day within a week — a column in the designer grid."""

    week = models.ForeignKey(
        Week,
        on_delete=models.CASCADE,
        related_name="sessions",
        verbose_name=_("Week"),
    )
    day_number = models.PositiveIntegerField(_("Day number"))
    name = models.CharField(_("Name"), max_length=255, blank=True)
    bias = models.CharField(_("Bias"), max_length=255, blank=True)
    order = models.PositiveIntegerField(_("Order"), default=0)

    class Meta:
        ordering = ["order", "day_number"]
        verbose_name = "Session"
        verbose_name_plural = "Sessions"

    def __str__(self):
        return f"Day {self.day_number} · {self.name}".rstrip(" ·")


class ExercisePrescription(models.Model):
    """A prescribed exercise row. Hybrid: catalog FK when matched, else free text (B4).

    ``sets``/``reps``/``load``/``rpe`` are free-form text — the prototype grid
    accepts ``load="BW"``, ``rpe="—"``, and rep ranges. Numeric coercion happens
    at read time (the designer JS already does this).
    """

    session = models.ForeignKey(
        Session,
        on_delete=models.CASCADE,
        related_name="prescriptions",
        verbose_name=_("Session"),
    )
    exercise = models.ForeignKey(
        "exercises.Exercise",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meso_prescriptions",
        verbose_name=_("Catalog exercise"),
    )
    name = models.CharField(_("Name"), max_length=255)
    order = models.PositiveIntegerField(_("Order"), default=0)
    sets = models.CharField(_("Sets"), max_length=32, blank=True)
    reps = models.CharField(_("Reps"), max_length=32, blank=True)
    load = models.CharField(_("Load"), max_length=32, blank=True)
    rpe = models.CharField(_("RPE"), max_length=32, blank=True)
    note = models.CharField(_("Note"), max_length=255, blank=True)
    tags = models.JSONField(_("Tags"), default=list, blank=True)

    class Meta:
        ordering = ["order"]
        verbose_name = "Exercise prescription"
        verbose_name_plural = "Exercise prescriptions"

    def __str__(self):
        return self.name

    @property
    def is_catalog_linked(self):
        """True when this row is backed by a catalog ``Exercise`` (B4 hybrid)."""
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
# Delivery / lightweight versioning (Phase 4)
#
# Delivering a week stamps ``Week.delivered_at`` and records a ``WeekDelivery``
# snapshot of the week at that moment. "Changes since last delivery" then =
# diff(current serialization, latest ``payload``); the full diff *UI* is
# deferred (persistence-plan open assumption #3) — this just captures the data
# cheaply.
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
# them is safe. See ``docs/meso/agent-plan.md``.
# ---------------------------------------------------------------------------


class AgentProposalBatch(models.Model):
    """One agent run: the coach's instruction + the batch of edits it proposed."""

    class Status(models.TextChoices):
        # The agent run happens off the request thread (Phase 4): a batch starts
        # DRAFTING, then the background job flips it to PENDING (ready for review)
        # or FAILED (with the reason in ``error``).
        DRAFTING = "drafting", _("Drafting")
        PENDING = "pending", _("Pending review")
        FAILED = "failed", _("Failed")
        APPLIED = "applied", _("Applied")
        DISMISSED = "dismissed", _("Dismissed")

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="proposal_batches",
        verbose_name=_("Plan"),
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
        ExercisePrescription,
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
        ExercisePrescription,
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

    class Meta:
        ordering = ["set_number"]
        verbose_name = "Logged set"
        verbose_name_plural = "Logged sets"

    def __str__(self):
        return f"Set {self.set_number}"


# ---------------------------------------------------------------------------
# Groups (S1) — Phase 1: the group + membership spine
#
# A coach groups several of their athletes who train together; later phases give
# the group a *shared program* (a ``Plan`` rooted at the group) and per-athlete
# *auto-adjusts* (override diffs — the ``adj`` overlay). Phase 1 is just the
# tenancy-correct foundation: a coach-owned ``MesoGroup`` and a
# ``GroupMembership`` linking it to an **active** ``CoachAthlete`` relationship,
# so membership structurally implies an active coaching link and per-athlete
# overrides/delivered plans (later) hang off the same relationship that owns
# individual plans (D-a). See ``docs/meso/groups-plan.md``.
# ---------------------------------------------------------------------------


class MesoGroupQuerySet(models.QuerySet):
    def for_coach(self, user):
        """Groups this coach owns."""
        return self.filter(coach=user)

    def active(self):
        """Non-archived groups (the roster shows these)."""
        return self.filter(status=MesoGroup.Status.ACTIVE)


class MesoGroup(models.Model):
    """A coach's training group — several athletes sharing one program (S1)."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        ACTIVE = "active", _("Active")
        ARCHIVED = "archived", _("Archived")

    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="meso_groups",
        verbose_name=_("Coach"),
    )
    name = models.CharField(_("Name"), max_length=255)
    focus = models.CharField(_("Focus"), max_length=255, blank=True)
    status = models.CharField(
        _("Status"), max_length=16, choices=Status, default=Status.ACTIVE
    )
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)

    objects = MesoGroupQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name = "Group"
        verbose_name_plural = "Groups"

    def __str__(self):
        return self.name

    def add_athlete(self, athlete):
        """Add one of the coach's *active* athletes to the group (idempotent).

        Membership hangs off the ``CoachAthlete`` link, so the athlete must have
        an active link with this group's coach — otherwise there is nothing to
        program against. Raises ``InvalidTransition`` if no such link exists
        (which also rejects a cross-coach athlete or the coach themselves).
        """
        link = (
            CoachAthlete.objects.for_coach(self.coach)
            .active()
            .filter(athlete=athlete)
            .first()
        )
        if link is None:
            raise InvalidTransition(
                "Can only add an athlete with an active link to this coach."
            )
        membership, _ = GroupMembership.objects.get_or_create(
            group=self, relationship=link
        )
        return membership

    def remove_athlete(self, athlete):
        """Remove an athlete from the group (a no-op if they aren't a member)."""
        self.memberships.filter(relationship__athlete=athlete).delete()

    def active_member_users(self):
        """The member athletes whose coaching link is still active, name-ordered.

        A membership whose link ended is hidden here but the row survives, so
        reopening the link restores the member (read-side scoping, not deletion).
        Scoped to *this* coach's links so a membership written outside
        ``add_athlete`` (e.g. a raw admin inline) can never leak a foreign coach's
        athlete onto the roster/detail page — defense in depth alongside
        ``GroupMembership.clean``.
        """
        return [
            m.relationship.athlete
            for m in self.memberships.select_related(
                "relationship", "relationship__athlete"
            )
            .prefetch_related("relationship__athlete__contraindications")
            .filter(
                relationship__coach=self.coach,
                relationship__status=CoachAthlete.Status.ACTIVE,
            )
            .order_by("relationship__athlete__name", "relationship__athlete__email")
        ]


class GroupMembership(models.Model):
    """A coach's athlete (via their ``CoachAthlete`` link) belonging to a group."""

    group = models.ForeignKey(
        MesoGroup,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name=_("Group"),
    )
    relationship = models.ForeignKey(
        CoachAthlete,
        on_delete=models.CASCADE,
        related_name="group_memberships",
        verbose_name=_("Relationship"),
    )
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Group membership"
        verbose_name_plural = "Group memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["group", "relationship"], name="unique_group_membership"
            ),
        ]

    def __str__(self):
        return f"{self.relationship.athlete.display_name()} ∈ {self.group.name}"

    def clean(self):
        """A membership's relationship must belong to the group's coach.

        ``add_athlete`` only ever creates same-coach memberships; this backstops
        the admin inline (which exposes a raw ``relationship``) so a coach can't
        attach another coach's athlete to their group.
        """
        if (
            self.group_id
            and self.relationship_id
            and self.relationship.coach_id != self.group.coach_id
        ):
            raise ValidationError(
                {
                    "relationship": _(
                        "The relationship must belong to the group's coach."
                    )
                }
            )


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
