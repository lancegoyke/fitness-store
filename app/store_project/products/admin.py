from django.contrib import admin, messages
from django.utils.translation import ngettext

from store_project.products.models import Category, Program


@admin.register(Program)
class ProgramAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "created",
        "views",
    ]
    prepopulated_fields = {"slug": ("name",)}
    ordering = [
        "-created",
    ]
    actions = [
        "make_public",
        "make_draft",
        "make_private",
    ]

    def make_public(self, request, queryset):
        updated = queryset.update(status=Program.PUBLIC)
        self.message_user(
            request,
            ngettext(
                f"{updated} program was successfully marked as public.",
                f"{updated} programs were successfully marked as public.",
                updated,
            ),
            messages.SUCCESS,
        )

    def make_draft(self, request, queryset):
        updated = queryset.update(status=Program.DRAFT)
        self.message_user(
            request,
            ngettext(
                f"{updated} program was successfully marked as draft.",
                f"{updated} programs were successfully marked as draft.",
                updated,
            ),
            messages.SUCCESS,
        )

    def make_private(self, request, queryset):
        updated = queryset.update(status=Program.PRIVATE)
        self.message_user(
            request,
            ngettext(
                f"{updated} program was successfully marked as private.",
                f"{updated} programs were successfully marked as private.",
                updated,
            ),
            messages.SUCCESS,
        )

    make_public.short_description = "Mark selected programs as public"
    make_draft.short_description = "Mark selected programs as draft"
    make_private.short_description = "Mark selected programs as private"


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name"]
    ordering = ["name"]
