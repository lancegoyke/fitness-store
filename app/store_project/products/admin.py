from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.utils.translation import ngettext

from store_project.products.models import (
    Category,
    Price,
    Program,
    Book,
    Subscription
)


User = get_user_model()


@admin.register(Program)
class ProgramAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "created",
        "views",
        "status",
    ]
    prepopulated_fields = {"slug": ("name",)}
    ordering = [
        "-created",
    ]
    actions = [
        # Disabled because bulk updates don't trigger django-lifecycle
        # AFTER_SAVE marketing emails.
        # "make_public",
        "make_draft",
        "make_private",
    ]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "author":
            kwargs["queryset"] = User.objects.filter(is_staff=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

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


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "created",
        "views",
        "status",
    ]
    prepopulated_fields = {"slug": ("name",)}
    ordering = [
        "-created",
    ]
    actions = [
        # Disabled because bulk updates don't trigger django-lifecycle
        # AFTER_SAVE marketing emails.
        # "make_public",
        "make_draft",
        "make_private",
    ]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "author":
            kwargs["queryset"] = User.objects.filter(is_staff=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def make_public(self, request, queryset):
        updated = queryset.update(status=Book.PUBLIC)
        self.message_user(
            request,
            ngettext(
                f"{updated} book was successfully marked as public.",
                f"{updated} books were successfully marked as public.",
                updated,
            ),
            messages.SUCCESS,
        )

    def make_draft(self, request, queryset):
        updated = queryset.update(status=Book.DRAFT)
        self.message_user(
            request,
            ngettext(
                f"{updated} book was successfully marked as draft.",
                f"{updated} books were successfully marked as draft.",
                updated,
            ),
            messages.SUCCESS,
        )

    def make_private(self, request, queryset):
        updated = queryset.update(status=Book.PRIVATE)
        self.message_user(
            request,
            ngettext(
                f"{updated} book was successfully marked as private.",
                f"{updated} books were successfully marked as private.",
                updated,
            ),
            messages.SUCCESS,
        )

    make_public.short_description = "Mark selected books as public"
    make_draft.short_description = "Mark selected books as draft"
    make_private.short_description = "Mark selected books as private"


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "created",
        "views",
        "status",
    ]
    prepopulated_fields = {"slug": ("name",)}
    ordering = [
        "-created",
    ]
    actions = [
        # Disabled because bulk updates don't trigger django-lifecycle
        # AFTER_SAVE marketing emails.
        # "make_public",
        "make_draft",
        "make_private",
    ]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "author":
            kwargs["queryset"] = User.objects.filter(is_staff=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def make_public(self, request, queryset):
        updated = queryset.update(status=Subscription.PUBLIC)
        self.message_user(
            request,
            ngettext(
                f"{updated} subscription was successfully marked as public.",
                f"{updated} subscriptions were successfully marked as public.",
                updated,
            ),
            messages.SUCCESS,
        )

    def make_draft(self, request, queryset):
        updated = queryset.update(status=Subscription.DRAFT)
        self.message_user(
            request,
            ngettext(
                f"{updated} subscription was successfully marked as draft.",
                f"{updated} subscriptions were successfully marked as draft.",
                updated,
            ),
            messages.SUCCESS,
        )

    def make_private(self, request, queryset):
        updated = queryset.update(status=Book.PRIVATE)
        self.message_user(
            request,
            ngettext(
                f"{updated} subscription was successfully marked as private.",
                f"{updated} subscriptions were successfully marked as private.",
                updated,
            ),
            messages.SUCCESS,
        )

    make_public.short_description = "Mark selected subscriptions as public"
    make_draft.short_description = "Mark selected subscriptions as draft"
    make_private.short_description = "Mark selected subscriptions as private"


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name"]
    ordering = ["name"]


@admin.register(Price)
class PriceAdmin(admin.ModelAdmin):
    list_display = [
        "nickname",
        "metadata",
        # "product",
        "price_type",
        "usage_type",
        "billing_scheme",
        "unit_amount",
        "tiers_mode",
        "interval",
        "interval_count",
        "aggregate_usage",
    ]
    ordering = ["-created"]
