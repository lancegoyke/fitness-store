from itertools import chain

from django.views.generic.base import TemplateView
from django.views.generic.detail import DetailView
from django.views.generic.edit import CreateView
from django.views.generic.list import ListView
from django.shortcuts import render

from markdownx.utils import markdownify

from .models import Book, Product, Program



class StoreView(TemplateView):
    template_name = 'products/store.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Staff should see everything
        if self.request.user.is_staff:
            programs = Program.objects.all()
            books = Book.objects.all()
            products = sorted(
                chain(programs, books),
                key=lambda product: product.created, reverse=True
            )
            context["products"] = products
        # Users should only see public items
        else:
            programs = Program.objects.filter(status=Program.PUBLIC)
            books = Book.objects.filter(status=Book.PUBLIC)
            products = sorted(
                chain(programs, books),
                key=lambda product: product.created, reverse=True
            )
            context["products"] = products
        
        context["products"] = products
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
