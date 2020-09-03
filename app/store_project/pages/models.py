from django.contrib.auth import get_user_model
from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _

from markdownx.models import MarkdownxField


class Page(models.Model):
    title = models.CharField(
        _("Page title"), max_length=settings.PRODUCT_NAME_MAX_LENGTH
    )
    content = MarkdownxField(_("Page content, in markdown"), default="")
    slug = models.SlugField(
        _("Slug for page"),
        default="",
        null=False,
        unique=True,
        max_length=settings.PRODUCT_NAME_MAX_LENGTH,
    )
    author = models.ForeignKey(get_user_model(), null=True, on_delete=models.SET_NULL)

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("pages:single", kwargs={"slug": self.slug})
