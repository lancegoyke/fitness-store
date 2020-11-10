import pytest

from django.test import RequestFactory
from django.urls import reverse, resolve

from store_project.pages.models import Page
from store_project.pages.factories import PageFactory


pytestmark = pytest.mark.django_db


def test_home_page_url():
    assert reverse("pages:home") == "/"
    assert resolve("/").view_name == "pages:home"


def test_single_page_url(page: Page):
    assert reverse("pages:single", kwargs={"slug": page.slug}) == f"/{page.slug}/"
    assert resolve(f"/{page.slug}/").view_name == "pages:single"
