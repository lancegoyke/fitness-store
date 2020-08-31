from django.urls import path

from .views import ProgramDetailView, ProgramListView


app_name = "products"
urlpatterns = [
    path("programs/<str:slug>/", ProgramDetailView.as_view(), name="program_detail"),
    path("programs/", ProgramListView.as_view(), name="program_list"),
]
