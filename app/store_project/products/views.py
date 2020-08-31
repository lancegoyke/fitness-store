from django.views.generic.detail import DetailView
from django.views.generic.edit import CreateView
from django.views.generic.list import ListView
from django.shortcuts import render

from .models import Program


class ProgramListView(ListView):
    model = Program


class ProgramDetailView(DetailView):
    model = Program
