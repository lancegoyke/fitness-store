from django.contrib.sitemaps import Sitemap
from store_project.exercises.models import Exercise


class ExerciseSitemap(Sitemap):
    priority = 0.5
    changefreq = "weekly"

    def items(self):
        return Exercise.objects.all()

    def lastmod(self, obj):
        return obj.modified
