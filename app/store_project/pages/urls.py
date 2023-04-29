from django.conf import settings
from django.urls import path
from django.views.decorators.cache import cache_page
from store_project.pages.views import HomePageView
from store_project.pages.views import SinglePageView
from store_project.pages.views import contact_view
from store_project.pages.views import robots_txt
from store_project.pages.views import timer_view

app_name = "pages"
urlpatterns = [
    path(
        "",
        cache_page(settings.DEFAULT_CACHE_TIMEOUT)(HomePageView.as_view()),
        name="home",
    ),
    path(
        "contact/",
        cache_page(settings.DEFAULT_CACHE_TIMEOUT)(contact_view),
        name="contact",
    ),
    path("timer/", timer_view, name="timer"),
    path("robots.txt", robots_txt, name="robots_txt"),
    path(
        "<str:slug>/",
        cache_page(settings.DEFAULT_CACHE_TIMEOUT)(SinglePageView.as_view()),
        name="single",
    ),
]
