from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from markdownx.models import MarkdownxField


class Page(models.Model):
    PUBLIC = "pb"
    PRIVATE = "pr"
    DRAFT = "dr"
    STATUS_CHOICES = [
        (PUBLIC, "Public"),
        (PRIVATE, "Private"),
        (DRAFT, "Draft"),
    ]
    status = models.CharField(
        max_length=2,
        choices=STATUS_CHOICES,
        default=DRAFT,
    )
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
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("pages:single", kwargs={"slug": self.slug})
