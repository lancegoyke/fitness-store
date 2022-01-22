import pytest

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import reverse

from store_project.users.models import User
from store_project.exercises.factories import ExerciseFactory
from store_project.exercises.models import Exercise
from store_project.exercises.views import (
    ExerciseListView,
    ExerciseDetailView,
)

pytestmark = pytest.mark.django_db


class TestExerciseDetailView:
    def test_authenticated(self, user: User, exercise: Exercise, rf: RequestFactory):
        request = rf.get(f"/exercises/{exercise.slug}/")
        request.user = user

        response = ExerciseDetailView.as_view()(request, slug=exercise.slug)

        admin_link = reverse("admin:exercises_exercise_change", args=(exercise.id,))

        assert response.status_code == 200
        assert "exercises/exercise_detail.html" in response.template_name
        assert exercise.get_yt_demo_id() in response.rendered_content
        assert exercise.get_yt_explan_id() in response.rendered_content
        assert admin_link not in response.rendered_content

    def test_super_authenticated(
        self, superuser: User, exercise: Exercise, rf: RequestFactory
    ):
        request = rf.get(f"/exercises/{exercise.slug}/")
        request.user = superuser

        response = ExerciseDetailView.as_view()(request, slug=exercise.slug)

        admin_link = reverse("admin:exercises_exercise_change", args=(exercise.id,))

        assert response.status_code == 200
        assert "exercises/exercise_detail.html" in response.template_name
        assert exercise.get_yt_demo_id() in response.rendered_content
        assert exercise.get_yt_explan_id() in response.rendered_content
        assert admin_link in response.rendered_content

    def test_not_authenticated(self, exercise: Exercise, rf: RequestFactory):
        request = rf.get(f"/exercises/{exercise.slug}/")
        request.user = AnonymousUser()

        response = ExerciseDetailView.as_view()(request, slug=exercise.slug)

        admin_link = reverse("admin:exercises_exercise_change", args=(exercise.id,))

        assert response.status_code == 200
        assert "exercises/exercise_detail.html" in response.template_name
        assert exercise.get_yt_demo_id() in response.rendered_content
        assert exercise.get_yt_explan_id() in response.rendered_content
        assert admin_link not in response.rendered_content


class TestExerciseListView:
    def test_authenticated(self, user: User, rf: RequestFactory):
        ex1 = ExerciseFactory()
        ex2 = ExerciseFactory()
        ex3 = ExerciseFactory()

        request = rf.get("/exercises/")
        request.user = user

        response = ExerciseListView.as_view()(request)

        exercises_admin_link = reverse("admin:exercises_exercise_changelist")

        assert response.status_code == 200
        assert "exercises/index.html" in response.template_name
        assert ex1.name in response.rendered_content
        assert ex2.name in response.rendered_content
        assert ex3.name in response.rendered_content
        assert exercises_admin_link not in response.rendered_content

    def test_super_authenticated(self, superuser: User, rf: RequestFactory):
        ex1 = ExerciseFactory()
        ex2 = ExerciseFactory()
        ex3 = ExerciseFactory()

        request = rf.get("/exercises/")
        request.user = superuser

        response = ExerciseListView.as_view()(request)

        exercises_admin_link = reverse("admin:exercises_exercise_changelist")

        assert response.status_code == 200
        assert "exercises/index.html" in response.template_name
        assert ex1.name in response.rendered_content
        assert ex2.name in response.rendered_content
        assert ex3.name in response.rendered_content
        assert exercises_admin_link in response.rendered_content

    def test_not_authenticated(self, rf: RequestFactory):
        ex1 = ExerciseFactory()
        ex2 = ExerciseFactory()
        ex3 = ExerciseFactory()

        request = rf.get("/exercises/")
        request.user = AnonymousUser()

        response = ExerciseListView.as_view()(request)

        exercises_admin_link = reverse("admin:exercises_exercise_changelist")

        assert response.status_code == 200
        assert "exercises/index.html" in response.template_name
        assert ex1.name in response.rendered_content
        assert ex2.name in response.rendered_content
        assert ex3.name in response.rendered_content
        assert exercises_admin_link not in response.rendered_content
