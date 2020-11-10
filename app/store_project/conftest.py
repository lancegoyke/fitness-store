import pytest

from store_project.users.models import User
from store_project.users.factories import SuperAdminFactory, UserFactory
from store_project.pages.models import Page
from store_project.pages.factories import PageFactory
from store_project.products.models import Program
from store_project.products.factories import ProgramFactory


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
def program() -> Program:
    return ProgramFactory()


@pytest.fixture
def page() -> Page:
    return PageFactory()