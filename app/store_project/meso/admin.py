from django.contrib import admin

from .models import AgentProposalBatch
from .models import AthleteOneRm
from .models import AthleteProfile
from .models import CoachAthlete
from .models import CoachInvite
from .models import CoachProfile
from .models import CoachSubscription
from .models import Contraindication
from .models import ExercisePrescription
from .models import GroupMembership
from .models import LoggedSet
from .models import Mesocycle
from .models import MesoGroup
from .models import Plan
from .models import PrescriptionOverride
from .models import ProposedChange
from .models import PushSubscription
from .models import Session
from .models import SessionLog
from .models import Week
from .models import WeekDelivery


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
class CoachAthleteAdmin(admin.ModelAdmin):
    list_display = (
        "coach",
        "athlete",
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
class PlanAdmin(admin.ModelAdmin):
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


@admin.register(Mesocycle)
class MesocycleAdmin(admin.ModelAdmin):
    list_display = ("name", "plan", "order", "week_count")
    raw_id_fields = ("plan",)
    inlines = (WeekInline,)


class SessionInline(admin.TabularInline):
    model = Session
    extra = 0


class WeekDeliveryInline(admin.TabularInline):
    model = WeekDelivery
    extra = 0
    fields = ("delivered_at", "created_at")
    readonly_fields = ("delivered_at", "created_at")
    can_delete = False


@admin.register(Week)
class WeekAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "phase",
        "volume",
        "intensity",
        "is_deload",
        "is_current",
        "delivered_at",
    )
    list_filter = ("is_deload", "is_current")
    raw_id_fields = ("mesocycle",)
    inlines = (SessionInline, WeekDeliveryInline)


@admin.register(WeekDelivery)
class WeekDeliveryAdmin(admin.ModelAdmin):
    list_display = ("__str__", "week", "delivered_at", "created_at")
    raw_id_fields = ("week",)
    readonly_fields = ("delivered_at", "payload", "created_at")


class ExercisePrescriptionInline(admin.TabularInline):
    model = ExercisePrescription
    extra = 0
    raw_id_fields = ("exercise",)


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "week", "day_number", "order")
    raw_id_fields = ("week",)
    inlines = (ExercisePrescriptionInline,)


@admin.register(ExercisePrescription)
class ExercisePrescriptionAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "session",
        "sets",
        "reps",
        "load",
        "load_type",
        "rpe",
        "is_catalog_linked",
    )
    list_filter = ("load_type",)
    search_fields = ("name",)
    raw_id_fields = ("session", "exercise")


class LoggedSetInline(admin.TabularInline):
    model = LoggedSet
    extra = 0
    raw_id_fields = ("prescription",)


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
class AgentProposalBatchAdmin(admin.ModelAdmin):
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


# -- groups (S1) -----------------------------------------------------------


class GroupMembershipInline(admin.TabularInline):
    model = GroupMembership
    extra = 0
    raw_id_fields = ("relationship",)
    readonly_fields = ("created_at",)


@admin.register(MesoGroup)
class MesoGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "coach", "focus", "status", "is_demo", "modified")
    list_filter = ("status", "is_demo")
    search_fields = ("name", "focus", "coach__email", "coach__name")
    raw_id_fields = ("coach",)
    inlines = (GroupMembershipInline,)


@admin.register(PrescriptionOverride)
class PrescriptionOverrideAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "membership",
        "swap_name",
        "load_pct",
        "sets",
        "reps",
        "modified",
    )
    search_fields = (
        "membership__group__name",
        "prescription__name",
        "swap_name",
    )
    raw_id_fields = ("membership", "prescription")
    readonly_fields = ("created_at", "modified")


# -- athlete PWA -----------------------------------------------------------


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "athlete", "created_at")
    search_fields = ("athlete__email", "athlete__name", "endpoint")
    raw_id_fields = ("athlete",)
    readonly_fields = ("created_at",)


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
