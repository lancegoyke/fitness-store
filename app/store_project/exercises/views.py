from django.shortcuts import get_object_or_404
from django.views.generic import DetailView, ListView

from store_project.exercises.models import Alternative, Category, Exercise


class ExerciseDetailView(DetailView):
    model = Exercise

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["alternatives"] = Alternative.objects.filter(original=self.get_object())
        return context


class ExerciseListView(ListView):
    model = Exercise
    context_object_name = "exercises"
    ordering = "name"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["categories"] = Category.objects.all()
        return context


class ExerciseFilteredListView(ListView):
    model = Exercise
    context_object_name = "exercises"
    ordering = "name"
    template_name = "exercises/exercise_filtered_list.html"

    def get_queryset(self):
        self.category = get_object_or_404(Category, slug=self.kwargs["category"])
        return Exercise.objects.filter(categories=self.category)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        self.category = get_object_or_404(Category, slug=self.kwargs["category"])
        context["category"] = self.category.name
        return context
