from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods
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
    template_name = "exercises/index.html"

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


@require_http_methods(["POST"])
def search(request):
    e_list = []
    search = request.POST["search"]
    if len(search) == 0:
        return render(
            request,
            "exercises/exercises.html",
            {
                "exercises": Exercise.objects.all().order_by("name"),
            })
    for e in Exercise.objects.all():
        if search.lower() in e.name.lower():
            e_list.append(e)
    return render(
        request,
        "exercises/exercises.html",
        {
            "exercises": e_list,
        })
