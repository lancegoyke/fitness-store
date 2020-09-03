from django.views.generic.detail import DetailView
from django.views.generic.edit import CreateView
from django.views.generic.list import ListView
from django.shortcuts import render

from markdownx.utils import markdownify

from .models import Program


class ProgramListView(ListView):
    model = Program


class ProgramDetailView(DetailView):
    model = Program
    context_object_name = "program"

    def get_context_data(self, **kwargs):
        context = super(ProgramDetailView, self).get_context_data(**kwargs)
        context["content"] = markdownify(self.object.page_content)
        return context
