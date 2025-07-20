from django.urls import path

from store_project.feed.views import LatestProductsFeed

app_name = "feed"
urlpatterns = [
    path("products/", LatestProductsFeed(), name="rss"),
]
