from django.contrib import admin

from markdownx.admin import MarkdownxModelAdmin

from .models import Page


@admin.register(Page)
class PageAdmin(MarkdownxModelAdmin):
    prepopulated_fields = {"slug": ("title",)}
