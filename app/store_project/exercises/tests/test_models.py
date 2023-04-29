import pytest
from store_project.exercises.factories import AlternativeFactory
from store_project.exercises.factories import ExerciseFactory
from store_project.exercises.models import Category
from store_project.exercises.models import Exercise

pytestmark = pytest.mark.django_db

yt_links = [
    "https://youtu.be/rTLSGke1AuA",
    "https://www.youtube.com/watch?v=rTLSGke1AuA",
    "https://www.youtube.com/watch?v=rTLSGke1AuA&list=PLHaGRfu0X0CJ1LBdXg-atqsLsKd7rYO0V&index=54",  # noqa: E501
]


@pytest.mark.parametrize("demonstration", yt_links)
def test_exercise_ytdemoid(demonstration):
    exercise = ExerciseFactory(demonstration=demonstration)
    assert exercise.get_yt_demo_id() == "rTLSGke1AuA"


@pytest.mark.parametrize("explanation", yt_links)
def test_exercise_ytexplanid(explanation):
    exercise = ExerciseFactory(explanation=explanation)
    assert exercise.get_yt_explan_id() == "rTLSGke1AuA"


def test_exercise_get_absolute_url(exercise: Exercise):
    assert exercise.get_absolute_url() == f"/exercises/{exercise.slug}/"


def test_alternative():
    alternative = AlternativeFactory(
        original=ExerciseFactory(), alternate=ExerciseFactory()
    )
    assert alternative.original
    assert alternative.alternate
    assert alternative.problem


def test_exercise_no_category(exercise: Exercise):
    assert not exercise.categories.exists()


def test_exercise_category(category: Category):
    exercise: Exercise = ExerciseFactory.create(categories=(category,))
    assert exercise.categories.exists()
