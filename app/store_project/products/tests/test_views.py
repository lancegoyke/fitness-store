import pytest
from django.contrib.auth.models import AnonymousUser
from django.http.response import Http404
from django.shortcuts import render
from django.test import RequestFactory
from django.urls import reverse

from store_project.users.models import User
from store_project.products.models import Book, Program
from store_project.products.views import (
    BookListView,
    BookDetailView,
    StoreView,
    ProgramListView,
    ProgramDetailView,
)

pytestmark = pytest.mark.django_db


class TestStoreView:
    def test_authenticated(
        self, user: User, program: Program, book: Book, rf: RequestFactory
    ):
        request = rf.get("/store/")
        request.user = AnonymousUser()

        response = StoreView.as_view()(request)

        books_admin_link = reverse("admin:products_book_changelist")
        programs_admin_link = reverse("admin:products_program_changelist")

        assert response.status_code == 200
        assert "products/store.html" in response.template_name
        assert response.context_data["products"]
        assert book.name in response.rendered_content
        assert book.description in response.rendered_content
        assert book.author.name in response.rendered_content
        assert program.name in response.rendered_content
        assert program.description in response.rendered_content
        assert program.author.name in response.rendered_content
        assert f'href="/books/{book.slug}/"' in response.rendered_content
        assert f'href="/programs/{program.slug}/"' in response.rendered_content
        assert programs_admin_link not in response.rendered_content
        assert books_admin_link not in response.rendered_content

    def test_not_authenticated(
        self, user: User, program: Program, book: Book, rf: RequestFactory
    ):
        request = rf.get("/store/")
        request.user = user

        response = StoreView.as_view()(request)

        books_admin_link = reverse("admin:products_book_changelist")
        programs_admin_link = reverse("admin:products_program_changelist")

        assert response.status_code == 200
        assert "products/store.html" in response.template_name
        assert response.context_data["products"]
        assert book.name in response.rendered_content
        assert book.description in response.rendered_content
        assert book.author.name in response.rendered_content
        assert program.name in response.rendered_content
        assert program.description in response.rendered_content
        assert program.author.name in response.rendered_content
        assert f'href="/books/{book.slug}/"' in response.rendered_content
        assert f'href="/programs/{program.slug}/"' in response.rendered_content
        assert programs_admin_link not in response.rendered_content
        assert books_admin_link not in response.rendered_content

    def test_super_authenticated(
        self, superuser: User, program: Program, book: Book, rf: RequestFactory
    ):
        request = rf.get("/store/")
        request.user = superuser

        response = StoreView.as_view()(request)

        books_admin_link = reverse("admin:products_book_changelist")
        programs_admin_link = reverse("admin:products_program_changelist")

        assert response.status_code == 200
        assert "products/store.html" in response.template_name
        assert response.context_data["products"]
        assert book.name in response.rendered_content
        assert book.description in response.rendered_content
        assert book.author.name in response.rendered_content
        assert program.name in response.rendered_content
        assert program.description in response.rendered_content
        assert program.author.name in response.rendered_content
        assert f'href="/books/{book.slug}/"' in response.rendered_content
        assert f'href="/programs/{program.slug}/"' in response.rendered_content
        assert programs_admin_link in response.rendered_content
        assert books_admin_link in response.rendered_content


class TestProgramDetailView:
    def test_authenticated(self, user: User, program: Program, rf: RequestFactory):
        request = rf.get(f"/programs/{program.slug}/")
        request.user = user

        response = ProgramDetailView.as_view()(request, slug=program.slug)

        login_to_purchase_link = f'href="/payments/login-to-purchase/{program.slug}/">'
        purchase_button_id = 'id="submitButton"'
        purchase_button_data_product_slug = f'data-product-slug="{program.slug}"'
        purchase_button_data_product_type = 'data-product-type="program"'
        admin_link = reverse("admin:products_program_change", args=(program.id,))

        assert response.status_code == 200
        assert "products/program_detail.html" in response.template_name
        assert response.context_data["content"]
        assert response.context_data["program"]
        assert purchase_button_id in response.rendered_content
        assert purchase_button_data_product_slug in response.rendered_content
        assert purchase_button_data_product_type in response.rendered_content
        assert login_to_purchase_link not in response.rendered_content
        assert admin_link not in response.rendered_content

    def test_not_authenticated(self, user: User, program: Program, rf: RequestFactory):
        request = rf.get(f"/programs/{program.slug}/")
        request.user = AnonymousUser()

        response = ProgramDetailView.as_view()(request, slug=program.slug)

        login_to_purchase_link = f'href="/payments/login-to-purchase/program/{program.slug}/">'
        purchase_button_id = 'id="submitButton"'
        purchase_button_data_product_slug = f'data-product-slug="{program.slug}"'
        purchase_button_data_product_type = 'data-product-type="program"'

        assert response.status_code == 200
        assert "products/program_detail.html" in response.template_name
        assert response.context_data["content"]
        assert response.context_data["program"]
        assert login_to_purchase_link in response.rendered_content
        assert purchase_button_id not in response.rendered_content
        assert purchase_button_data_product_slug not in response.rendered_content
        assert purchase_button_data_product_type not in response.rendered_content

    def test_super_authenticated(
        self, superuser: User, program: Program, rf: RequestFactory
    ):
        request = rf.get(f"/programs/{program.slug}/")
        request.user = superuser

        response = ProgramDetailView.as_view()(request, slug=program.slug)

        login_to_purchase_link = f'href="/payments/login-to-purchase/program/{program.slug}/">'
        purchase_button_id = 'id="submitButton"'
        purchase_button_data_product_slug = f'data-product-slug="{program.slug}"'
        purchase_button_data_product_type = 'data-product-type="program"'
        admin_link = reverse("admin:products_program_change", args=(program.id,))

        assert response.status_code == 200
        assert "products/program_detail.html" in response.template_name
        assert response.context_data["content"]
        assert response.context_data["program"]
        assert purchase_button_id not in response.rendered_content
        assert purchase_button_data_product_slug not in response.rendered_content
        assert purchase_button_data_product_type not in response.rendered_content
        assert login_to_purchase_link not in response.rendered_content
        assert admin_link in response.rendered_content


class TestProgramListView:
    def test_authenticated(self, user: User, program: Program, rf: RequestFactory):
        request = rf.get("/programs/")
        request.user = AnonymousUser()

        response = ProgramListView.as_view()(request)

        admin_link = reverse("admin:products_program_changelist")

        assert response.status_code == 200
        assert "products/program_list.html" in response.template_name
        assert response.context_data["programs"]
        assert program.name in response.rendered_content
        assert program.description in response.rendered_content
        assert program.author.name in response.rendered_content
        assert f'href="/programs/{program.slug}/"' in response.rendered_content
        assert admin_link not in response.rendered_content

    def test_not_authenticated(self, user: User, program: Program, rf: RequestFactory):
        request = rf.get("/programs/")
        request.user = user

        response = ProgramListView.as_view()(request)

        admin_link = reverse("admin:products_program_changelist")

        assert response.status_code == 200
        assert "products/program_list.html" in response.template_name
        assert response.context_data["programs"]
        assert program.name in response.rendered_content
        assert program.description in response.rendered_content
        assert program.author.name in response.rendered_content
        assert f'href="/programs/{program.slug}/"' in response.rendered_content
        assert admin_link not in response.rendered_content

    def test_super_authenticated(
        self, superuser: User, program: Program, rf: RequestFactory
    ):
        request = rf.get("/programs/")
        request.user = superuser

        response = ProgramListView.as_view()(request)

        admin_link = reverse("admin:products_program_changelist")

        assert response.status_code == 200
        assert "products/program_list.html" in response.template_name
        assert response.context_data["programs"]
        assert program.name in response.rendered_content
        assert program.description in response.rendered_content
        assert program.author.name in response.rendered_content
        assert f'href="/programs/{program.slug}/"' in response.rendered_content
        assert admin_link in response.rendered_content


class TestBookDetailView:
    def test_authenticated(self, user: User, book: Book, rf: RequestFactory):
        request = rf.get(f"/books/{book.slug}/")
        request.user = user

        response = BookDetailView.as_view()(request, slug=book.slug)

        login_to_purchase_link = f'href="/payments/login-to-purchase/{book.slug}/">'
        purchase_button_id = 'id="submitButton"'
        purchase_button_data_product_slug = f'data-product-slug="{book.slug}"'
        purchase_button_data_product_type = 'data-product-type="book"'
        admin_link = reverse("admin:products_book_change", args=(book.id,))

        assert response.status_code == 200
        assert "products/book_detail.html" in response.template_name
        assert response.context_data["content"]
        assert response.context_data["book"]
        assert purchase_button_id in response.rendered_content
        assert purchase_button_data_product_slug in response.rendered_content
        assert purchase_button_data_product_type in response.rendered_content
        assert login_to_purchase_link not in response.rendered_content
        assert admin_link not in response.rendered_content

    def test_not_authenticated(self, user: User, book: Book, rf: RequestFactory):
        request = rf.get(f"/books/{book.slug}/")
        request.user = AnonymousUser()

        response = BookDetailView.as_view()(request, slug=book.slug)

        login_to_purchase_link = f'href="/payments/login-to-purchase/book/{book.slug}/">'
        purchase_button_id = 'id="submitButton"'
        purchase_button_data_product_slug = f'data-product-slug="{book.slug}"'
        purchase_button_data_product_type = 'data-product-type="book"'

        assert response.status_code == 200
        assert "products/book_detail.html" in response.template_name
        assert response.context_data["content"]
        assert response.context_data["book"]
        assert login_to_purchase_link in response.rendered_content
        assert purchase_button_id not in response.rendered_content
        assert purchase_button_data_product_slug not in response.rendered_content
        assert purchase_button_data_product_type not in response.rendered_content

    def test_super_authenticated(
        self, superuser: User, book: Book, rf: RequestFactory
    ):
        request = rf.get(f"/books/{book.slug}/")
        request.user = superuser

        response = BookDetailView.as_view()(request, slug=book.slug)

        login_to_purchase_link = f'href="/payments/login-to-purchase/book/{book.slug}/">'
        purchase_button_id = 'id="submitButton"'
        purchase_button_data_product_slug = f'data-product-slug="{book.slug}"'
        purchase_button_data_product_type = 'data-product-type="book"'
        admin_link = reverse("admin:products_book_change", args=(book.id,))

        assert response.status_code == 200
        assert "products/book_detail.html" in response.template_name
        assert response.context_data["content"]
        assert response.context_data["book"]
        assert purchase_button_id not in response.rendered_content
        assert purchase_button_data_product_slug not in response.rendered_content
        assert purchase_button_data_product_type not in response.rendered_content
        assert login_to_purchase_link not in response.rendered_content
        assert admin_link in response.rendered_content


class TestBookListView:
    def test_authenticated(self, user: User, book: Book, rf: RequestFactory):
        request = rf.get("/books/")
        request.user = AnonymousUser()

        response = BookListView.as_view()(request)

        admin_link = reverse("admin:products_book_changelist")

        assert response.status_code == 200
        assert "products/book_list.html" in response.template_name
        assert response.context_data["books"]
        assert book.name in response.rendered_content
        assert book.description in response.rendered_content
        assert book.author.name in response.rendered_content
        assert f'href="/books/{book.slug}/"' in response.rendered_content
        assert admin_link not in response.rendered_content

    def test_not_authenticated(self, user: User, book: Book, rf: RequestFactory):
        request = rf.get("/books/")
        request.user = user

        response = BookListView.as_view()(request)

        admin_link = reverse("admin:products_book_changelist")

        assert response.status_code == 200
        assert "products/book_list.html" in response.template_name
        assert response.context_data["books"]
        assert book.name in response.rendered_content
        assert book.description in response.rendered_content
        assert book.author.name in response.rendered_content
        assert f'href="/books/{book.slug}/"' in response.rendered_content
        assert admin_link not in response.rendered_content

    def test_super_authenticated(
        self, superuser: User, book: Book, rf: RequestFactory
    ):
        request = rf.get("/books/")
        request.user = superuser

        response = BookListView.as_view()(request)

        admin_link = reverse("admin:products_book_changelist")

        assert response.status_code == 200
        assert "products/book_list.html" in response.template_name
        assert response.context_data["books"]
        assert book.name in response.rendered_content
        assert book.description in response.rendered_content
        assert book.author.name in response.rendered_content
        assert f'href="/books/{book.slug}/"' in response.rendered_content
        assert admin_link in response.rendered_content
