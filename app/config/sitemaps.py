from django.contrib import sitemaps
from django.urls import reverse


class StaticViewSitemap(sitemaps.Sitemap):
    priority = 0.5
    changefreq = "daily"

    def items(self):
        return ["products:store", "products:program_list", "products:book_list",]

    def location(self, item):
        return reverse(item)
