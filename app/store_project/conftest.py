import pytest

from store_project.users.models import User
from store_project.users.factories import SuperAdminFactory, UserFactory
from store_project.pages.models import Page
from store_project.pages.factories import PageFactory
from store_project.products.models import Book, Program
from store_project.products.factories import BookFactory, ProgramFactory
from store_project.exercises.models import Category, Exercise
from store_project.exercises.factories import CategoryFactory, ExerciseFactory


@pytest.fixture(autouse=True)
def media_storage(settings, tmpdir):
    settings.MEDIA_ROOT = tmpdir.strpath


@pytest.fixture
def user() -> User:
    return UserFactory()


@pytest.fixture
def superuser() -> User:
    return SuperAdminFactory()


@pytest.fixture
def book() -> Book:
    return BookFactory()


@pytest.fixture
def program() -> Program:
    return ProgramFactory()


@pytest.fixture
def page() -> Page:
    return PageFactory()


@pytest.fixture
def exercise() -> Exercise:
    return ExerciseFactory()


@pytest.fixture
def category() -> Category:
    return CategoryFactory()
