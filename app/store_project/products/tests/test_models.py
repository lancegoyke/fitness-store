import pytest

from django.contrib.auth.models import Permission

from store_project.products.factories import BookFactory, ProgramFactory
from store_project.products.models import Book, Product, Program

pytestmark = pytest.mark.django_db


def test_program_get_absolute_url(program: Program):
    assert program.get_absolute_url() == f"/programs/{program.slug}/"


def test_program_is_public(program: Program):
    public_program = program
    assert public_program.is_public()
    draft_program = ProgramFactory(status=Product.DRAFT)
    assert not draft_program.is_public()
    private_program = ProgramFactory(status=Product.PRIVATE)
    assert not private_program.is_public()


def test_program_add_permission(program: Program):
    """Has lifecycle hook created a Permission for this program?"""
    assert Permission.objects.get(codename=f"can_view_{program.slug}")
    assert Permission.objects.get(name=f"Can view {program.name}")


def test_program_remove_permission(program: Program):
    """Bypassing the lifecycle hook due to errors with Stripe."""
    program.remove_program_permission()
    with pytest.raises(Permission.DoesNotExist):
        assert Permission.objects.get(codename=f"can_view_{program.slug}")
        assert Permission.objects.get(name=f"Can view {program.name}")


def test_book_get_absolute_url(book: Book):
    assert book.get_absolute_url() == f"/books/{book.slug}/"


def test_book_is_public(book: Book):
    public_book = book
    assert public_book.is_public()
    draft_book = BookFactory(status=Book.DRAFT)
    assert not draft_book.is_public()
    private_book = BookFactory(status=Book.PRIVATE)
    assert not private_book.is_public()


def test_book_add_permission(book: Book):
    """Has lifecycle hook created a Permission for this book?"""
    assert Permission.objects.get(codename=f"can_view_{book.slug}")
    assert Permission.objects.get(name=f"Can view {book.name}")


def test_book_remove_permission(book: Book):
    """Bypassing the lifecycle hook due to errors with Stripe."""
    book.remove_book_permission()
    with pytest.raises(Permission.DoesNotExist):
        assert Permission.objects.get(codename=f"can_view_{book.slug}")
        assert Permission.objects.get(name=f"Can view {book.name}")
