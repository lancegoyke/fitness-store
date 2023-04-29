from django.views.generic.base import TemplateView
from django.views.generic.detail import DetailView
from django.views.generic.list import ListView
from markdownx.utils import markdownify

from .models import Book, Program


class StoreView(TemplateView):
    template_name = "products/product_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if self.request.user.is_staff:
            # Staff should see everything
            programs = Program.objects.all().order_by("name")
            books = Book.objects.all().order_by("name")
        else:
            # Users should only see public items
            programs = Program.objects.filter(status=Program.PUBLIC).order_by("name")
            books = Book.objects.filter(status=Book.PUBLIC).order_by("name")

        context["programs"] = programs
        context["books"] = books

        return context


class ProgramListView(ListView):
    model = Program
    context_object_name = "programs"
    template_name = "products/program_list.html"

    def get_queryset(self):
        if self.request.user.is_staff:
            return Program.objects.all()
        else:
            return Program.objects.filter(status=Program.PUBLIC)


class ProgramDetailView(DetailView):
    model = Program
    context_object_name = "program"
    template_name = "products/program_detail.html"

    def get_context_data(self, **kwargs):
        context = super(ProgramDetailView, self).get_context_data(**kwargs)
        context["content"] = markdownify(self.object.page_content)
        return context


class BookListView(ListView):
    model = Book
    context_object_name = "books"
    template_name = "products/book_list.html"

    def get_queryset(self):
        if self.request.user.is_staff:
            return Book.objects.all()
        else:
            return Book.objects.filter(status=Book.PUBLIC)


class BookDetailView(DetailView):
    model = Book
    context_object_name = "book"
    template_name = "products/book_detail.html"

    def get_context_data(self, **kwargs):
        context = super(BookDetailView, self).get_context_data(**kwargs)
        context["content"] = markdownify(self.object.page_content)
        return context
