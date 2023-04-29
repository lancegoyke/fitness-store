from django.contrib.sitemaps import Sitemap
from store_project.products.models import Book, Program


class BookSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.5

    def items(self):
        return Book.objects.filter(status=Book.PUBLIC)

    def lastmod(self, obj):
        return obj.modified


class ProgramSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.5

    def items(self):
        return Program.objects.filter(status=Program.PUBLIC)

    def lastmod(self, obj):
        return obj.modified
