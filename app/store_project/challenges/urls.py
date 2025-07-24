from django.urls import path

from .views import ChallengeCreateView
from .views import ChallengeDetail
from .views import challenge_filtered_list

app_name = "challenges"

urlpatterns = [
    path("<str:slug>", ChallengeDetail.as_view(), name="challenge_detail"),
    path("new/", ChallengeCreateView.as_view(), name="challenge_create"),
    path("tag/<str:slug>", challenge_filtered_list, name="challenge_tag_filtered_list"),
    path("tag/", challenge_filtered_list, name="challenge_tag_list"),
    path("", challenge_filtered_list, name="challenge_filtered_list"),
]
