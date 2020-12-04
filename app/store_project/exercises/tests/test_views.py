import pytest
from django.http.response import Http404
from django.shortcuts import render
from django.test import RequestFactory
from django.urls import reverse

from store_project.exercises.models import Exercise
from store_project.exercises.views import (
    ExerciseListView,
    ExerciseDetailView,
)

pytestmark = pytest.mark.django_db


class TestExerciseDetailView:
    def test_authenticated(self, exercise: Exercise, rf: RequestFactory):
        request = rf.get(f"/exercises/{exercise.slug}/")

        response = ExerciseDetailView.as_view()(request, slug=exercise.slug)

        assert response.status_code == 200
        assert "exercises/exercise_detail.html" in response.template_name
