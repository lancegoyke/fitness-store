import pytest

from django.test import RequestFactory

from store_project.pages.models import Page
from store_project.pages.factories import PageFactory
from store_project.pages.views import HomePageView, SinglePageView


pytestmark = pytest.mark.django_db


def test_home_page_view(rf: RequestFactory):
    request = rf.get(f"/")

    response = HomePageView.as_view()(request)

    assert response.status_code == 200
    assert "pages/home.html" in response.template_name


def test_single_page_view(rf: RequestFactory):
    page = PageFactory(content="### Markdown Title")
    request = rf.get(f"/{page.slug}/")

    response = SinglePageView.as_view()(request, slug=page.slug)

    assert response.status_code == 200
    assert "pages/single.html" in response.template_name
    assert "<h3>Markdown Title</h3>" in response.rendered_content
    assert f"<h1>{page.title}</h1>" in response.rendered_content
