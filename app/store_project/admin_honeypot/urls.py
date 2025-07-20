from django.urls import path
from django.urls import re_path

from store_project.admin_honeypot import views

app_name = "admin_honeypot"

urlpatterns = [
    path("login/", views.AdminHoneypot.as_view(), name="login"),
    re_path(r"^.*$", views.AdminHoneypot.as_view(), name="index"),
]
