from django.contrib import admin

from .models import AthleteProfile
from .models import CoachAthlete
from .models import CoachProfile
from .models import Contraindication


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
    list_display = ("__str__", "user", "training_started", "modified")
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
        "created_at",
        "responded_at",
    )
    list_filter = ("status", "invited_by")
    search_fields = (
        "coach__email",
        "coach__name",
        "athlete__email",
        "athlete__name",
    )
    raw_id_fields = ("coach", "athlete")
    readonly_fields = ("token", "created_at", "responded_at", "ended_at")
