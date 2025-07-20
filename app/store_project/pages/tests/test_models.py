import pytest

from store_project.pages.models import Page

pytestmark = pytest.mark.django_db


def test_str(page: Page):
    assert str(page) == f"{page.title}"


def test_get_absolute_url(page: Page):
    assert page.get_absolute_url() == f"/{page.slug}/"
