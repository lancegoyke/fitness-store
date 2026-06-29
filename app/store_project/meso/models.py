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


class LoadType(models.TextChoices):
    """What a prescription's Load *number* means (units & RPE/%1RM slice, S2).

    ``ABSOLUTE`` — a weight in the plan's ``Unit`` (kg/lb); the default, so every
    existing row keeps reading exactly as before. ``PERCENT`` — a percentage of
    1RM, rendered with a ``%`` suffix instead of the unit. RPE is orthogonal (its
    own column, and coexists with either load type).
    """

    ABSOLUTE = "abs", _("Absolute load")
    PERCENT = "pct", _("% of 1RM")


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
        """Individual plans this coach owns through an *active* relationship (N2/D-a).

        Deliberately *individual-only*: it backs the deliver / results / review
        flows, which assume a single athlete. Group plans (rooted at a
        ``MesoGroup``) are reached via ``editable_by`` instead, so they never leak
        into an athlete-shaped flow. A *materialized* group-delivery plan
        (``source_group`` set — a member's resolved snapshot, groups Phase 4) is
        excluded too: it is athlete-facing only, never something the coach designs
        or delivers directly.
        """
        return self.filter(
            relationship__coach=user,
            relationship__status=CoachAthlete.Status.ACTIVE,
            source_group__isnull=True,
        )

    def for_athlete(self, user):
        """Plans across all of the athlete's active coaches (individual only).

        Includes a member's *materialized* group-delivery plan (``source_group``
        set) — that resolved snapshot is exactly what the athlete should see for a
        group they train in (groups Phase 4).
        """
        return self.filter(
            relationship__athlete=user,
            relationship__status=CoachAthlete.Status.ACTIVE,
        )

    def editable_by(self, user):
        """Plans this coach may open + edit in the designer (individual *or* group).

        The designer + autosave surface, where a coach works on a plan they own:
        an individual plan through an *active* relationship, or a *group* plan
        through owning the group. Group plans stay out of ``for_coach`` (the
        individual-only deliver/results/review flows) — this is the wider gate for
        the one screen that handles both. A materialized group-delivery plan
        (``source_group`` set) is excluded: the coach edits the *shared* program,
        never a member's derived snapshot. See ``docs/meso/groups-plan.md``.
        """
        return self.filter(
            models.Q(
                relationship__coach=user,
                relationship__status=CoachAthlete.Status.ACTIVE,
                source_group__isnull=True,
            )
            | models.Q(group__coach=user)
        )

    def active(self):
        return self.filter(status=Plan.Status.ACTIVE)


class Plan(models.Model):
    """A periodized training plan, rooted at either a relationship or a group.

    Most plans are *individual* — owned by one coach↔athlete ``relationship``
    (D-a). A *group* plan (groups slice S1, Phase 2) is instead rooted at a
    ``MesoGroup``: one shared program several athletes train off, with per-athlete
    auto-adjusts layered on later (Phase 3). Exactly one of ``relationship`` /
    ``group`` is set — a DB ``XOR`` constraint — so the whole program tree
    (Mesocycle → … → ExercisePrescription) is reused for both, gaining only a root
    and (later) an override overlay, not a second hierarchy.
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
    group = models.ForeignKey(
        "MesoGroup",
        on_delete=models.CASCADE,
        related_name="plans",
        verbose_name=_("Group"),
        null=True,
        blank=True,
    )
    # Provenance (groups Phase 4), *distinct* from the XOR root above: when set,
    # this is a member's **materialized** group-delivery plan — an individual plan
    # (rooted at ``relationship``) whose week mirrors the group's delivered week
    # with that member's overrides resolved in. One per (member, group), refreshed
    # on re-delivery. Hidden from the coach's individual surfaces (``for_coach`` /
    # ``editable_by``); the athlete sees it like any individual plan.
    source_group = models.ForeignKey(
        "MesoGroup",
        on_delete=models.CASCADE,
        related_name="materialized_plans",
        verbose_name=_("Source group"),
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
            models.CheckConstraint(
                # Exactly one root: an individual plan has a relationship, a group
                # plan has a group, never both and never neither.
                condition=(
                    models.Q(relationship__isnull=False, group__isnull=True)
                    | models.Q(relationship__isnull=True, group__isnull=False)
                ),
                name="plan_relationship_xor_group",
            ),
            models.UniqueConstraint(
                # A member's materialized group-delivery plan is a singleton:
                # re-delivery refreshes the same row, never spawns a second.
                fields=["relationship", "source_group"],
                condition=models.Q(source_group__isnull=False),
                name="unique_materialized_group_plan",
            ),
        ]

    def __str__(self):
        if self.group_id is not None:
            return f"{self.title} ({self.group.name})"
        return f"{self.title} ({self.athlete.display_name()})"

    @property
    def is_group(self):
        """True when this plan is a group's shared program (not an individual)."""
        return self.group_id is not None

    @property
    def coach(self):
        """The coach who owns this plan — via the relationship or the group."""
        if self.group_id is not None:
            return self.group.coach
        return self.relationship.coach

    @property
    def athlete(self):
        """The single athlete (individual plans only); None for a group plan."""
        if self.relationship_id is None:
            return None
        return self.relationship.athlete

    def is_editable_by(self, user):
        """Whether ``user`` (a coach) may open + edit this plan in the designer.

        An individual plan is editable by its coach over an *active* relationship;
        a group plan by the coach who owns the group. A *materialized*
        group-delivery plan (``source_group`` set) is never editable — it is a
        derived snapshot the coach manages only through the shared program, so the
        autosave / deliver / agent endpoints (which gate on this) reject it.
        Mirrors ``PlanQuerySet.editable_by`` for a single fetched plan.
        """
        if self.relationship_id is not None:
            return (
                self.source_group_id is None
                and self.relationship.coach_id == user.id
                and self.relationship.is_active
            )
        if self.group_id is not None:
            return self.group.coach_id == user.id
        return False


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
    # What ``load`` means: an absolute weight (the plan's unit) or a % of 1RM (S2).
    load_type = models.CharField(
        _("Load type"), max_length=3, choices=LoadType, default=LoadType.ABSOLUTE
    )
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

    @classmethod
    def create_for_coach(cls, coach, *, name, focus="", athletes=()):
        """Create a new group for ``coach`` and add the given athletes (S1 Phase 2b).

        The create-group entry point from the roster. ``name`` is required by the
        caller (the view rejects a blank one); ``focus`` is optional. Each athlete
        is added via ``add_athlete``, so the same active-link tenancy guard
        applies — one without an active link to this coach is skipped rather than
        raising, since the create form only ever offers the coach's own athletes
        and a stale/foreign pick shouldn't fail the whole create.
        """
        group = cls.objects.create(coach=coach, name=name, focus=focus)
        for athlete in athletes:
            try:
                group.add_athlete(athlete)
            except InvalidTransition:
                continue
        return group

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
        """Remove an athlete from the group (a no-op if they aren't a member).

        Also **archives** any materialized group-delivery plan this athlete holds
        from this group. The membership row gated the *fan-out*, but the athlete's
        *visibility* of an already-delivered program comes from the active
        ``Plan(relationship=…, source_group=self)`` (``for_athlete`` returns it
        regardless of membership) — so removing a delivered member must archive
        that snapshot or they keep seeing and logging it on ``/meso/me/``. A
        re-add + re-deliver restores it (``sync_delivered_plan`` reactivates).
        """
        self.materialized_plans.filter(
            relationship__athlete=athlete, relationship__coach=self.coach
        ).update(status=Plan.Status.ARCHIVED)
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

    def active_memberships(self):
        """Memberships whose coaching link is still active, name-ordered.

        The membership analogue of ``active_member_users`` (the fan-out reads each
        member's *overrides* off the membership, so it needs the rows, not just the
        users). Scoped to *this* coach's active links so a membership written
        outside ``add_athlete`` can never deliver to a foreign coach's athlete.
        """
        return list(
            self.memberships.select_related("relationship", "relationship__athlete")
            .filter(
                relationship__coach=self.coach,
                relationship__status=CoachAthlete.Status.ACTIVE,
            )
            .order_by("relationship__athlete__name", "relationship__athlete__email")
        )

    def deliver_current_week(self, plan=None):
        """Fan the shared program's current week out to every active member (Phase 4).

        ``plan`` pins *which* of the group's plans to deliver — the endpoint passes
        the exact plan the request addressed so it can't drift to a different one
        when a group holds more than one program; it defaults to ``shared_plan()``
        (the canonical current one) for the seed / server button. A plan that
        isn't this group's is rejected.

        Each active member gets their *resolved* week (the shared template + their
        override diffs) materialized into their own individual plan and stamped
        ``delivered_at``, plus a ``WeekDelivery`` snapshot; the shared (group) week
        is stamped too as the coach-side record. Returns ``(now, [(member_plan,
        member_week), ...])`` so a caller (the view) can notify each athlete — the
        model layer stays request-free so the seed can reuse it.

        Raises ``InvalidTransition`` when there is nothing to deliver: no shared
        program, no current week, or no active members (delivering to nobody is a
        coach mistake, surfaced as a 400 / flashed error rather than a silent no-op).
        """
        # Local import avoids a models→serializers cycle at module load.
        from .serializers import current_week
        from .serializers import serialize_week_snapshot

        if plan is None:
            plan = self.shared_plan()
        if plan is None or plan.group_id != self.pk:
            raise InvalidTransition("The group has no shared program to deliver.")
        group_week = current_week(plan)
        if group_week is None:
            raise InvalidTransition("The shared program has no week to deliver.")
        memberships = self.active_memberships()
        if not memberships:
            raise InvalidTransition("The group has no members to deliver to.")

        now = timezone.now()
        delivered = []
        for membership in memberships:
            member_plan, member_week = membership.sync_delivered_plan(group_week)
            member_week.delivered_at = now
            member_week.save(update_fields=["delivered_at"])
            WeekDelivery.objects.create(
                week=member_week,
                delivered_at=now,
                payload=serialize_week_snapshot(member_week),
            )
            delivered.append((member_plan, member_week))
        group_week.delivered_at = now
        group_week.save(update_fields=["delivered_at"])
        WeekDelivery.objects.create(
            week=group_week,
            delivered_at=now,
            payload=serialize_week_snapshot(group_week),
        )
        # Bump the shared plan so it stays the coach's working plan after a deliver.
        plan.save(update_fields=["modified"])
        return now, delivered

    def shared_plan(self):
        """The group's current shared program (most-recent non-archived), or None.

        A group can accumulate plans over time the way a relationship can; the
        designer entry point opens the live one (Phase 2). ``None`` when the group
        has no program yet — the detail page then offers to design one.
        """
        return (
            self.plans.exclude(status=Plan.Status.ARCHIVED)
            .order_by("-modified")
            .first()
        )

    def create_shared_plan(self):
        """Create the group's shared program rooted at the group, with a scaffold.

        Seeds a minimal-but-usable structure (one block, the current week, two
        training days each with a starter row) so the designer opens onto an
        editable grid — there is no add-session / add-week endpoint yet, so a
        bare plan would be uneditable. The coach then shapes it (and, Phase 3,
        layers per-athlete auto-adjusts).
        """
        profile = getattr(self.coach, "coach_profile", None)
        unit = profile.default_unit if profile else Unit.KILOGRAMS
        plan = Plan.objects.create(
            group=self,
            title=f"{self.name} — Shared program",
            goal=self.focus,
            status=Plan.Status.DRAFT,
            unit=unit,
        )
        mesocycle = Mesocycle.objects.create(
            plan=plan, name="Block 1", order=0, week_count=4
        )
        week = Week.objects.create(
            mesocycle=mesocycle,
            index=1,
            phase="Accum",
            volume=70,
            intensity=65,
            is_current=True,
        )
        for day in (1, 2):
            session = Session.objects.create(
                week=week, day_number=day, name=f"Day {day}", order=day - 1
            )
            ExercisePrescription.objects.create(
                session=session,
                name="New exercise",
                order=0,
                sets="3",
                reps="10",
                rpe="7",
            )
        return plan


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

    # -- per-athlete overrides (groups Phase 3) ---------------------------

    def set_override(
        self, prescription, *, swap_name="", load_pct=None, sets="", reps="", note=""
    ):
        """Upsert this member's auto-adjust for one shared-program prescription.

        A member's effective program = the shared template **+** their override
        diffs (the ``adj`` overlay). The prescription must live in *this* group's
        shared program (the same group the membership belongs to) — otherwise the
        override would target a lift the member doesn't train (raises
        ``InvalidTransition``, mirroring ``add_athlete``'s tenancy guard).

        ``load_pct`` of 100 is a no-op (100% of the shared load) and is dropped.
        An override with no remaining diff is meaningless, so it clears any
        existing row and returns ``None`` instead of storing a no-op.
        """
        if prescription.session.week.mesocycle.plan.group_id != self.group_id:
            raise InvalidTransition(
                "The prescription is not in this group's shared program."
            )
        if load_pct == 100:
            load_pct = None
        diff = {
            "swap_name": swap_name,
            "load_pct": load_pct,
            "sets": sets,
            "reps": reps,
            "note": note,
        }
        if not PrescriptionOverride.has_diff_from(diff):
            self.clear_override(prescription)
            return None
        override, _ = PrescriptionOverride.objects.update_or_create(
            membership=self, prescription=prescription, defaults=diff
        )
        return override

    def clear_override(self, prescription):
        """Drop this member's override for a prescription (a no-op if none)."""
        self.overrides.filter(prescription=prescription).delete()

    # -- delivery fan-out (groups Phase 4) --------------------------------

    def sync_delivered_plan(self, group_week):
        """Materialize/refresh this member's resolved copy of ``group_week`` (Phase 4).

        Get-or-creates the member's individual plan for this group (rooted at their
        relationship, tagged ``source_group``) and syncs its current week *in
        place* to the resolved group week — the shared template **+** this member's
        override diffs (``resolve_prescription``). Syncing in place (sessions
        matched by ``day_number``, prescriptions by ``order``) preserves any
        ``SessionLog`` the athlete already wrote against an unchanged row while
        propagating the coach's edits; a row dropped from the shared program drops
        here too. Returns ``(member_plan, member_week)`` — the week is *not* yet
        stamped delivered (``deliver_current_week`` stamps + snapshots it).
        """
        from .serializers import resolve_prescription

        shared_plan = group_week.mesocycle.plan
        member_plan, _ = Plan.objects.get_or_create(
            relationship=self.relationship,
            source_group=self.group,
            defaults={
                "title": shared_plan.title,
                "goal": shared_plan.goal,
                "unit": shared_plan.unit,
                "status": Plan.Status.ACTIVE,
            },
        )
        # Keep the snapshot's identity + active status current (a reopened link's
        # plan may have been archived by ``CoachAthlete.end``); the full ``save``
        # bumps ``modified`` so the athlete's home orders it freshly.
        member_plan.title = shared_plan.title
        member_plan.goal = shared_plan.goal
        member_plan.unit = shared_plan.unit
        member_plan.status = Plan.Status.ACTIVE
        member_plan.save()

        src_meso = group_week.mesocycle
        member_meso, _ = Mesocycle.objects.update_or_create(
            plan=member_plan,
            order=src_meso.order,
            defaults={"name": src_meso.name, "week_count": src_meso.week_count},
        )
        member_week, _ = Week.objects.update_or_create(
            mesocycle=member_meso,
            index=group_week.index,
            defaults={
                "phase": group_week.phase,
                "volume": group_week.volume,
                "intensity": group_week.intensity,
                "is_deload": group_week.is_deload,
                "is_current": True,
            },
        )

        overrides = {o.prescription_id: o for o in self.overrides.all()}
        src_sessions = list(group_week.sessions.prefetch_related("prescriptions"))
        for src_session in src_sessions:
            member_session, _ = Session.objects.update_or_create(
                week=member_week,
                day_number=src_session.day_number,
                defaults={
                    "name": src_session.name,
                    "bias": src_session.bias,
                    "order": src_session.order,
                },
            )
            src_orders = []
            for src_p in src_session.prescriptions.all():
                resolved = resolve_prescription(src_p, overrides.get(src_p.pk))
                # A swap means the catalog FK no longer names the lift trained.
                swapped = resolved["name"] != src_p.name
                ExercisePrescription.objects.update_or_create(
                    session=member_session,
                    order=src_p.order,
                    defaults={
                        "name": resolved["name"],
                        "sets": resolved["sets"],
                        "reps": resolved["reps"],
                        "load": resolved["load"],
                        "load_type": resolved["load_type"],
                        "rpe": resolved["rpe"],
                        "note": resolved["note"],
                        "exercise": None if swapped else src_p.exercise,
                        "tags": src_p.tags,
                    },
                )
                src_orders.append(src_p.order)
            member_session.prescriptions.exclude(order__in=src_orders).delete()
        src_day_numbers = [s.day_number for s in src_sessions]
        member_week.sessions.exclude(day_number__in=src_day_numbers).delete()
        return member_plan, member_week

    def clean(self):
        """Backstop ``add_athlete``'s contract on the admin inline (raw FKs).

        ``add_athlete`` only ever creates a membership off the group coach's
        *active* link, so ``clean`` enforces the same two rules for any path that
        runs ``full_clean`` (the admin):

        - the relationship must belong to the group's coach (never another
          coach's athlete);
        - on **creation**, the relationship must be active — adding a member via a
          pending/ended link would just be hidden by ``active_member_users``.

        The active check is creation-only on purpose: a membership row outlives
        its link ending (the row persists so reopening the link restores the
        member — read-side scoping, not deletion), so re-saving an existing row
        whose link has since ended must stay valid.
        """
        if not (self.group_id and self.relationship_id):
            return
        if self.relationship.coach_id != self.group.coach_id:
            raise ValidationError(
                {
                    "relationship": _(
                        "The relationship must belong to the group's coach."
                    )
                }
            )
        if (
            self._state.adding
            and self.relationship.status != CoachAthlete.Status.ACTIVE
        ):
            raise ValidationError(
                {"relationship": _("The relationship must be active to add a member.")}
            )


class PrescriptionOverride(models.Model):
    """One member's auto-adjust over a group's shared prescription (S1 Phase 3).

    A group plan is a *shared* program every member trains off; this overlay lets
    a coach diverge one member's row from the shared base — a contraindication
    swap, a per-athlete load %, or a volume tweak — without forking the program.
    A member's effective program = the shared template **+** their override diffs
    (the designer's per-row ``adj`` badge). The override hangs off the
    ``GroupMembership`` (and so, transitively, the same ``CoachAthlete`` link that
    owns the member's individual plans — D-a), and targets a prescription in the
    membership's group's shared program (a same-group invariant enforced by
    ``GroupMembership.set_override`` and ``clean``). One override per
    member+prescription. See ``docs/meso/groups-plan.md``.
    """

    # The widest sane load scaling — a 50% deload through a +100% bump. Outside
    # this is almost certainly a client bug; the endpoint rejects it as a 400.
    MIN_LOAD_PCT = 1
    MAX_LOAD_PCT = 200

    membership = models.ForeignKey(
        GroupMembership,
        on_delete=models.CASCADE,
        related_name="overrides",
        verbose_name=_("Membership"),
    )
    prescription = models.ForeignKey(
        ExercisePrescription,
        on_delete=models.CASCADE,
        related_name="overrides",
        verbose_name=_("Prescription"),
    )
    # The exercise this member trains instead (the "swap"); blank = the shared one.
    swap_name = models.CharField(_("Swap exercise"), max_length=255, blank=True)
    # Scale the shared numeric load to this percentage (90 = −10%); null = no change.
    load_pct = models.PositiveSmallIntegerField(_("Load %"), null=True, blank=True)
    # Per-athlete volume; blank inherits the shared sets/reps.
    sets = models.CharField(_("Sets"), max_length=32, blank=True)
    reps = models.CharField(_("Reps"), max_length=32, blank=True)
    note = models.CharField(_("Note"), max_length=255, blank=True)
    created_at = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)

    class Meta:
        ordering = ["prescription_id", "membership_id"]
        verbose_name = "Prescription override"
        verbose_name_plural = "Prescription overrides"
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "prescription"],
                name="unique_prescription_override",
            ),
        ]

    def __str__(self):
        return f"{self.membership.relationship.athlete.display_name()} · {self.prescription.name}"

    @staticmethod
    def has_diff_from(diff):
        """Whether an override dict carries a real adjust (vs an empty no-op)."""
        return bool(
            diff.get("swap_name")
            or diff.get("sets")
            or diff.get("reps")
            or diff.get("note")
            or diff.get("load_pct") is not None
        )

    @property
    def has_diff(self):
        """Whether this override carries a real adjust (vs an empty no-op)."""
        return self.has_diff_from(
            {
                "swap_name": self.swap_name,
                "load_pct": self.load_pct,
                "sets": self.sets,
                "reps": self.reps,
                "note": self.note,
            }
        )

    def clean(self):
        """Backstop the same-group invariant on the admin (raw FKs).

        ``set_override`` only ever targets a prescription in the membership's
        group's shared program; ``clean`` enforces the same for any path that runs
        ``full_clean`` (the admin) so an override can never point at a lift the
        member doesn't train.
        """
        if not (self.membership_id and self.prescription_id):
            return
        plan_group_id = self.prescription.session.week.mesocycle.plan.group_id
        if plan_group_id != self.membership.group_id:
            raise ValidationError(
                {
                    "prescription": _(
                        "The prescription must belong to the membership's group's "
                        "shared program."
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
# ``one_rm.py`` (derive/refresh/read) and ``docs/meso/one-rm-plan.md``.
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
    """

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
