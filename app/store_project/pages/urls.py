from django.urls import path

from store_project.pages.views import HomePageView, SinglePageView

app_name = "pages"
urlpatterns = [
    path("", HomePageView.as_view(), name="home"),
    path("<str:slug>/", SinglePageView.as_view(), name="single"),
]
