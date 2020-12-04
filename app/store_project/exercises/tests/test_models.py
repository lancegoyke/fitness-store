import pytest

from store_project.exercises.models import Exercise
from store_project.exercises.factories import ExerciseFactory

pytestmark = pytest.mark.django_db


def test_exercise_get_absolute_url(exercise: Exercise):
    assert exercise.get_absolute_url() == f"/exercises/{exercise.slug}/"


def test_exercise_category(exercise: Exercise):
    pass
