from django.contrib.sitemaps import Sitemap

from store_project.pages.models import Page


class PageSitemap(Sitemap):
    priority = 0.5
    changefreq = "daily"

    def items(self):
        return Page.objects.filter(status=Page.PUBLIC)
