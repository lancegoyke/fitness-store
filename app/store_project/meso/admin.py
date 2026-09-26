from django.contrib import admin
from django.db import transaction

from . import demo as meso_demo
from .models import AgentProposalBatch
from .models import AthleteOneRm
from .models import AthleteProfile
from .models import CoachAthlete
from .models import CoachInvite
from .models import CoachProfile
from .models import CoachSubscription
from .models import Contraindication
from .models import ExerciseSlot
from .models import LoggedSet
from .models import Mesocycle
from .models import Plan
from .models import Prescription
from .models import ProposedChange
from .models import PushSubscription
from .models import Session
from .models import SessionLog
from .models import SessionSlot
from .models import TourEvent
from .models import Week
from .models import WeekDelivery


class CascadeLockDeleteMixin:
    """Pre-lock a hard delete's cascade parents in the app-wide order (#587)."""

    cascade_lock_helper = None
    lock_delete_coach_mutexes = False

    def _lock_delete_roots(self, pks):
        # LOCK ORDER (#610) — a User selection can contain a coach and their
        # lower-pk demo athlete. Reserve selected coach mutexes before the
        # globally sorted cascade pass so it cannot invert against clear_demo.
        # This assumes #614's precondition: a demo athlete is never itself a coach.
        if self.lock_delete_coach_mutexes:
            meso_demo.lock_coach_mutexes(pks)
        getattr(meso_demo, self.cascade_lock_helper)(pks)

    def delete_model(self, request, obj):
        # LOCK ORDER (#587) — admin delete_view already owns an outer atomic
        # block, but keeping this boundary here also makes direct/custom admin
        # calls hold every parent reservation through the actual cascade.
        with transaction.atomic():
            pks = [obj.pk]
            self._lock_delete_roots(pks)
            return super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # LOCK ORDER (#587) — delete_selected calls this outside a transaction.
        # Resolve the roots after entering atomic, then keep their top-down
        # reservations until the queryset cascade has finished.
        with transaction.atomic():
            pks = list(queryset.order_by("pk").values_list("pk", flat=True))
            self._lock_delete_roots(pks)
            return super().delete_queryset(request, queryset)


@admin.register(CoachProfile)
class CoachProfileAdmin(admin.ModelAdmin):
    list_display = ("__str__", "user", "default_unit", "modified")
    search_fields = ("display_name", "user__email", "user__name")
    raw_id_fields = ("user",)


class ContraindicationInline(admin.TabularInline):
    model = Contraindication
    extra = 0
    raw_id_fields = ("athlete",)


@admin.register(AthleteProfile)
class AthleteProfileAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "user",
        "training_started",
        "delivery_email_opt_out",
        "modified",
    )
    list_filter = ("delivery_email_opt_out",)
    search_fields = ("user__email", "user__name")
    raw_id_fields = ("user",)


@admin.register(Contraindication)
class ContraindicationAdmin(admin.ModelAdmin):
    list_display = ("text", "athlete", "active", "created_at")
    list_filter = ("active",)
    search_fields = ("text", "athlete__email", "athlete__name")
    raw_id_fields = ("athlete",)


@admin.register(CoachAthlete)
class CoachAthleteAdmin(CascadeLockDeleteMixin, admin.ModelAdmin):
    cascade_lock_helper = "lock_cascade_from_links"
    list_display = (
        "coach",
        "athlete",
        "label",
        "status",
        "invited_by",
        "is_demo",
        "created_at",
        "responded_at",
    )
    list_filter = ("status", "invited_by", "is_demo")
    search_fields = (
        "coach__email",
        "coach__name",
        "athlete__email",
        "athlete__name",
    )
    raw_id_fields = ("coach", "athlete")
    readonly_fields = ("token", "created_at", "responded_at", "ended_at")


@admin.register(CoachInvite)
class CoachInviteAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "label",
        "coach",
        "status",
        "created_at",
        "expires_at",
        "reminder_sent_at",
    )
    list_filter = ("status",)
    search_fields = ("email", "coach__email", "coach__name")
    raw_id_fields = ("coach", "accepted_by", "accepted_link")
    readonly_fields = ("token", "created_at", "responded_at")


@admin.register(CoachSubscription)
class CoachSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "coach",
        "status",
        "quantity",
        "trial_end",
        "current_period_end",
        "modified",
    )
    list_filter = ("status",)
    search_fields = (
        "coach__email",
        "coach__name",
        "stripe_subscription_id",
    )
    raw_id_fields = ("coach",)
    readonly_fields = ("created", "modified")


# -- program schema --------------------------------------------------------


class MesocycleInline(admin.TabularInline):
    model = Mesocycle
    extra = 0


@admin.register(Plan)
class PlanAdmin(CascadeLockDeleteMixin, admin.ModelAdmin):
    cascade_lock_helper = "lock_cascade_from_plans"
    list_display = ("title", "relationship", "status", "unit", "modified")
    list_filter = ("status", "unit")
    search_fields = (
        "title",
        "relationship__coach__email",
        "relationship__athlete__email",
    )
    raw_id_fields = ("relationship",)
    inlines = (MesocycleInline,)


class WeekInline(admin.TabularInline):
    model = Week
    extra = 0


class SessionSlotInline(admin.TabularInline):
    model = SessionSlot
    extra = 0
    # #578 C1: an inline delete calls ``obj.delete()`` straight from
    # ``BaseModelFormSet.save_existing_objects()`` — no confirmation page
    # renders, so the "CASCADE is the loud option" story in
    # ``LoggedSet.exercise_slot``'s model comment only holds on this model's
    # OWN admin page (``SessionSlotAdmin``), not here. Mirrors
    # ``WeekDeliveryInline``'s ``can_delete = False``.
    can_delete = False


@admin.register(Mesocycle)
class MesocycleAdmin(admin.ModelAdmin):
    list_display = ("name", "plan", "order", "week_count")
    raw_id_fields = ("plan",)
    inlines = (SessionSlotInline, WeekInline)


class ExerciseSlotInline(admin.TabularInline):
    model = ExerciseSlot
    extra = 0
    raw_id_fields = ("exercise",)
    # #578 C1: same reasoning as ``SessionSlotInline`` — an inline delete
    # skips the confirmation page entirely, so a single Save here would
    # silently CASCADE to this slot's ``Prescription`` and ``LoggedSet`` rows
    # (an athlete's performed history) with no warning shown. The loud path
    # stays this model's OWN admin page (``ExerciseSlotAdmin``).
    can_delete = False


@admin.register(SessionSlot)
class SessionSlotAdmin(admin.ModelAdmin):
    """The fixed DAY definition (P0 fixed-lineup cutover)."""

    list_display = ("__str__", "mesocycle", "day_number", "name", "bias", "order")
    raw_id_fields = ("mesocycle",)
    inlines = (ExerciseSlotInline,)


@admin.register(ExerciseSlot)
class ExerciseSlotAdmin(admin.ModelAdmin):
    """The fixed EXERCISE row (P0 fixed-lineup cutover)."""

    list_display = ("name", "session_slot", "order", "is_catalog_linked")
    search_fields = ("name", "session_slot__name")
    raw_id_fields = ("session_slot", "exercise")


class SessionInline(admin.TabularInline):
    model = Session
    extra = 0
    raw_id_fields = ("session_slot",)


class WeekDeliveryInline(admin.TabularInline):
    model = WeekDelivery
    extra = 0
    fields = ("delivered_at", "created_at")
    readonly_fields = ("delivered_at", "created_at")
    can_delete = False


class PrescriptionInline(admin.TabularInline):
    """One week's cells — the ``ExercisePrescription`` inline's replacement.

    ``Prescription`` FKs to ``Week`` directly (not ``Session``), so this is a
    ``WeekAdmin`` inline rather than a ``SessionAdmin`` one.
    """

    model = Prescription
    extra = 0
    raw_id_fields = ("exercise_slot",)


@admin.register(Week)
class WeekAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "phase",
        "volume",
        "intensity",
        "is_deload",
        "delivered_at",
    )
    list_filter = ("is_deload",)
    raw_id_fields = ("mesocycle",)
    inlines = (SessionInline, PrescriptionInline, WeekDeliveryInline)


@admin.register(WeekDelivery)
class WeekDeliveryAdmin(admin.ModelAdmin):
    list_display = ("__str__", "week", "delivered_at", "created_at")
    raw_id_fields = ("week",)
    readonly_fields = ("delivered_at", "payload", "created_at")


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "week", "session_slot", "day_number", "order")
    raw_id_fields = ("week", "session_slot")


@admin.register(Prescription)
class PrescriptionAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "exercise_slot",
        "week",
        "line",
        "text",
        "skipped",
        "is_catalog_linked",
    )
    list_filter = ("skipped", "line")
    # ``name``/``is_catalog_linked`` are resolving properties, not DB columns —
    # search/filter against the real fields instead (the cell's own ``text`` or
    # the slot's ``name``).
    search_fields = ("text", "exercise_slot__name")
    raw_id_fields = ("exercise_slot", "week")


class LoggedSetInline(admin.TabularInline):
    model = LoggedSet
    extra = 0
    raw_id_fields = ("prescription", "source_line")
    # ``exercise_slot`` (#578 C1) is DERIVED, not edited — but the trade is
    # different depending on whether this row's ``prescription`` is live.
    #
    # While ``prescription`` (which IS editable here) is live: ``save()``
    # re-derives ``exercise_slot`` from it on every save (not just when it's
    # left blank — see that method's docstring), so an editable raw-id box
    # for ``exercise_slot`` could only ever contribute a value that
    # disagrees with the row's own cell for one request — the next save
    # overwrites it back into agreement. Shown, not editable.
    #
    # For a ``prescription``-NULL orphan (a #577/#581 hard-delete, or the
    # blank-add case below): ``save()``'s re-derive is guarded by
    # ``if self.prescription_id is not None``, so it does NOT fire, and an
    # editable box would in fact be the only way to re-attach an identity to
    # such a row. Leaving it readonly here anyway is a deliberate choice,
    # not an oversight — it matches migration 0051's own refusal to guess a
    # slot for an unrecoverable row from ``source_line``/``reclaimed_line``:
    # hand-typing a slot id in the admin would invent an identity the system
    # never actually observed, which is worse than leaving the row
    # unattached and countable as orphaned.
    #
    # The one shape that really is uncountable, and worth naming rather than
    # leaving implicit: an inline ADD with ``prescription`` also left blank
    # commits both pointers NULL, and no later save repairs that (there is no
    # ``prescription`` to derive from, and re-attaching by hand is the same
    # invented-identity problem as the paragraph above). Not a regression
    # introduced here — ``main`` produces an equally uncountable row from the
    # same blank add — just a gap this field doesn't close either.
    #
    # ``reclaimed_line`` is an internal hint for the restore lookup (#541),
    # not something to edit either. It has no DB constraint, so it can
    # outlive its cell; as an editable field that stale id would fail
    # validation and block saving the whole log.
    readonly_fields = ("exercise_slot", "reclaimed_line")


@admin.register(SessionLog)
class SessionLogAdmin(admin.ModelAdmin):
    list_display = ("__str__", "athlete", "date", "status")
    list_filter = ("status",)
    raw_id_fields = ("session", "athlete")
    inlines = (LoggedSetInline,)


# -- agent proposals -------------------------------------------------------


class ProposedChangeInline(admin.TabularInline):
    model = ProposedChange
    extra = 0
    fields = ("kind", "title", "status", "honors", "order")
    raw_id_fields = ("session", "prescription")


@admin.register(AgentProposalBatch)
class AgentProposalBatchAdmin(CascadeLockDeleteMixin, admin.ModelAdmin):
    cascade_lock_helper = "lock_cascade_from_batches"
    list_display = (
        "__str__",
        "plan",
        "coach",
        "status",
        "trigger",
        "model",
        "estimated_cost_usd",
        "created_at",
    )
    list_filter = ("status", "trigger", "billing_status", "model")
    search_fields = (
        "plan__title",
        "coach__email",
        "coach__name",
        "instruction",
        "request_id",
    )
    raw_id_fields = ("plan", "coach")
    # The usage/cost columns are captured by the agent run — read-only here so the
    # admin can inspect per-run cost without hand-editing the ledger.
    readonly_fields = (
        "error",
        "created_at",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "api_calls",
        "request_id",
        "stop_reason",
        "duration_ms",
        "estimated_cost_usd",
        "trigger",
        "billing_status",
    )
    inlines = (ProposedChangeInline,)


@admin.register(ProposedChange)
class ProposedChangeAdmin(admin.ModelAdmin):
    list_display = ("title", "batch", "kind", "status", "honors", "order")
    list_filter = ("kind", "status")
    search_fields = ("title", "rationale")
    raw_id_fields = ("batch", "session", "prescription")


# -- athlete PWA -----------------------------------------------------------


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "athlete", "created_at")
    search_fields = ("athlete__email", "athlete__name", "endpoint")
    raw_id_fields = ("athlete",)
    readonly_fields = ("created_at",)


# -- guided demo onboarding tour funnel events (#430 Phase 4) --------------
# No dashboard yet (a follow-up) — the owner reads the funnel here or via a
# shell query (e.g. `TourEvent.objects.values("kind").annotate(Count("id"))`).


@admin.register(TourEvent)
class TourEventAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "kind",
        "variant",
        "step_key",
        "segment",
        "coach",
        "created",
    )
    list_filter = ("kind", "variant", "step_key")
    search_fields = ("coach__email", "coach__name", "step_key", "segment")
    raw_id_fields = ("coach",)
    readonly_fields = ("created",)
    date_hierarchy = "created"


# -- persisted estimated 1RM (S2 follow-up) --------------------------------


@admin.register(AthleteOneRm)
class AthleteOneRmAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "athlete",
        "name",
        "value",
        "unit",
        "source",
        "updated_at",
    )
    list_filter = ("source", "unit")
    search_fields = ("athlete__email", "athlete__name", "name")
    raw_id_fields = ("athlete", "exercise")
    readonly_fields = ("key", "created_at", "updated_at")
