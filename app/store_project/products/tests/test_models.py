import pytest
import stripe
from django.contrib.auth.models import Permission

from store_project.products.factories import BookFactory
from store_project.products.factories import ProgramFactory
from store_project.products.models import Book
from store_project.products.models import Product
from store_project.products.models import Program

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


def test_delete_program_with_stripe_price_marks_product_and_price_inactive(
    program: Program,
):
    """#548: deleting a Program deactivates its Stripe Product and Price.

    stripe 15's ``Product.modify``/``Price.modify`` take the id positionally
    (not as ``sid=``), and the Price id is the *price's* id, not the
    product's.
    """
    program_id = str(program.id)  # capture before delete() clears the pk
    price_id = program.stripe_price_id
    assert price_id  # the BEFORE_CREATE hook set this via the mocked Stripe API

    stripe.Product.modify.reset_mock()
    stripe.Price.modify.reset_mock()

    program.delete()

    stripe.Product.modify.assert_called_once_with(program_id, active=False)
    stripe.Price.modify.assert_called_once_with(price_id, active=False)
    assert not Program.objects.filter(id=program_id).exists()


def test_delete_program_without_stripe_price_skips_price_modify(program: Program):
    """#548: an empty stripe_price_id must never be sent to Stripe.Price.modify."""
    program.stripe_price_id = ""
    program.save()
    program_id = str(program.id)

    stripe.Product.modify.reset_mock()
    stripe.Price.modify.reset_mock()

    program.delete()

    stripe.Product.modify.assert_called_once_with(program_id, active=False)
    stripe.Price.modify.assert_not_called()
    assert not Program.objects.filter(id=program_id).exists()


def test_delete_book_marks_stripe_product_inactive(book: Book):
    """#548: the shared BEFORE_DELETE hook on Product also works for Book."""
    book_id = str(book.id)

    stripe.Product.modify.reset_mock()
    stripe.Price.modify.reset_mock()

    book.delete()

    stripe.Product.modify.assert_called_once_with(book_id, active=False)
    assert not Book.objects.filter(id=book_id).exists()


def test_update_program_name_calls_stripe_product_modify(program: Program):
    """#548: update_product_in_stripe must pass id positionally, not as sid=."""
    stripe.Product.modify.reset_mock()

    program.name = "New Name"
    program.save()

    stripe.Product.modify.assert_called_once_with(
        str(program.id), name="New Name", description=program.description
    )
