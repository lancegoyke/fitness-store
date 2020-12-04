from django.contrib.sitemaps import Sitemap

from store_project.pages.models import Page


class PageSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.5

    def items(self):
        return Page.objects.filter(status=Page.PUBLIC)
