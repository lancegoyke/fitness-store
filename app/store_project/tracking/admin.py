from django.contrib import admin

from store_project.tracking.models import Category, Test


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "created")
    prepopulated_fields = {"slug": ("name",)}
