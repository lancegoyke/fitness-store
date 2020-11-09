import pytest

from django.urls import resolve, reverse

from store_project.products.models import Program
from store_project.users.models import User

pytestmark = pytest.mark.django_db


def test_program_list():
    assert reverse("products:program_list") == "/programs/"
    assert resolve("/programs/").view_name == "products:program_list"


def test_program_detail(program: Program):
    assert (
        reverse("products:program_detail", kwargs={"slug": program.slug})
        == f"/programs/{program.slug}/"
    )
    assert resolve(f"/programs/{program.slug}/").view_name == "products:program_detail"
