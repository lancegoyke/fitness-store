from django.contrib import admin
from django.contrib.auth import get_user_model

from markdownx.admin import MarkdownxModelAdmin

from .models import Page


User = get_user_model()


@admin.register(Page)
class PageAdmin(MarkdownxModelAdmin):
    prepopulated_fields = {"slug": ("title",)}

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "author":
            kwargs["queryset"] = User.objects.filter(is_staff=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
