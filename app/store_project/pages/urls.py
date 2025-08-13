from django.urls import path

from store_project.pages.views import HomePageView
from store_project.pages.views import SinglePageView
from store_project.pages.views import contact_view
from store_project.pages.views import robots_txt
from store_project.pages.views import timer_view

app_name = "pages"
urlpatterns = [
    path(
        "",
        HomePageView.as_view(),
        name="home",
    ),
    path(
        "contact/",
        contact_view,
        name="contact",
    ),
    path("timer/", timer_view, name="timer"),
    path("robots.txt", robots_txt, name="robots_txt"),
    path(
        "<str:slug>/",
        SinglePageView.as_view(),
        name="single",
    ),
]
