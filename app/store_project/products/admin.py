from django.contrib import admin

from store_project.products.models import Program


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
