import pytest
from django.urls import resolve
from django.urls import reverse
from store_project.products.models import Book
from store_project.products.models import Program

pytestmark = pytest.mark.django_db


def test_store():
    assert reverse("products:store") == "/store/"
    assert resolve("/store/").view_name == "products:store"


def test_program_list():
    assert reverse("products:program_list") == "/programs/"
    assert resolve("/programs/").view_name == "products:program_list"


def test_program_detail(program: Program):
    assert (
        reverse("products:program_detail", kwargs={"slug": program.slug})
        == f"/programs/{program.slug}/"
    )
    assert resolve(f"/programs/{program.slug}/").view_name == "products:program_detail"


def test_book_list():
    assert reverse("products:book_list") == "/books/"
    assert resolve("/books/").view_name == "products:book_list"


def test_book_detail(book: Book):
    assert (
        reverse("products:book_detail", kwargs={"slug": book.slug})
        == f"/books/{book.slug}/"
    )
    assert resolve(f"/books/{book.slug}/").view_name == "products:book_detail"
