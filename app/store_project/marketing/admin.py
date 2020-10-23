from django.contrib import admin

from markdownx.admin import MarkdownxModelAdmin

from .models import Email


@admin.register(Email)
class EmailAdmin(MarkdownxModelAdmin):
    pass
