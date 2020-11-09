import pytest

from store_project.users.models import User
from store_project.users.factories import UserFactory
from store_project.products.models import Program
from store_project.products.factories import ProgramFactory


@pytest.fixture(autouse=True)
def media_storage(settings, tmpdir):
    settings.MEDIA_ROOT = tmpdir.strpath


@pytest.fixture
def user() -> User:
    return UserFactory()


@pytest.fixture
def program() -> Program:
    return ProgramFactory()