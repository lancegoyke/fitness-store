import pytest

from django.urls import resolve, reverse

from store_project.products.models import Program
from store_project.users.models import User

pytestmark = pytest.mark.django_db


def test_program_list():
    assert reverse("products:program_list") == "/programs/"
    assert resolve("/programs/").view_name == "products:program_list"
