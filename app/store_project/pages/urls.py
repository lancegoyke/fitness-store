from django.urls import path

from store_project.pages.views import HomePageView, SinglePageView, robots_txt

app_name = "pages"
urlpatterns = [
    path("", HomePageView.as_view(), name="home"),
    path("robots.txt", robots_txt, name="robots_txt"),
    path("<str:slug>/", SinglePageView.as_view(), name="single"),
]
