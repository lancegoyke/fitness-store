from django.conf import settings
from django.urls import path
from django.views.decorators.cache import cache_page

from .views import (
    BookDetailView,
    BookListView,
    StoreView,
    ProgramDetailView,
    ProgramListView,
)


app_name = "products"
urlpatterns = [
    path(
        "store/",
        cache_page(settings.DEFAULT_CACHE_TIMEOUT)(StoreView.as_view()),
        name="store"
    ),
    path(
        "books/<str:slug>/",
        cache_page(settings.DEFAULT_CACHE_TIMEOUT)(BookDetailView.as_view()),
        name="book_detail"
    ),
    path(
        "books/",
        cache_page(settings.DEFAULT_CACHE_TIMEOUT)(BookListView.as_view()),
        name="book_list"
    ),
    path(
        "programs/<str:slug>/",
        cache_page(settings.DEFAULT_CACHE_TIMEOUT)(ProgramDetailView.as_view()),
        name="program_detail"
    ),
    path(
        "programs/",
        cache_page(settings.DEFAULT_CACHE_TIMEOUT)(ProgramListView.as_view()),
        name="program_list"
    ),
]
