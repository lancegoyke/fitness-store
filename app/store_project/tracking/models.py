from django.db import models

from users.models import User


class Category(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("name").desc(),
                "category",
                name="%(app_name)s_%(class)s_unique_lower_name",
            )
        ]


class Test(models.Model):
    """
    Fitness tests
    """
    name = models.CharField(max_length=200)
    description = models.TextField(null=True, default=None)
    video_link = models.URLField(null=True, default=None)
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)
    author = models.ForeignKey(
        User,
        verbose_name=_("Author of product"),
        null=True,
        on_delete=models.SET_NULL,
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_DEFAULT,
        default=Category.objects.get(name="uncategorized")
    )

    def __str__(self):
        return f"{self.name}"
    