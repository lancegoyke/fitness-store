from django.urls import path
from store_project.users.views import user_profile_view
from store_project.users.views import user_update_view

app_name = "users"
urlpatterns = [
    path("profile/", view=user_profile_view, name="profile"),
    path("update/", view=user_update_view, name="update"),
]
