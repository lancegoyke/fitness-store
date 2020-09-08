from django.contrib import admin

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


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name"]
    ordering = ["name"]