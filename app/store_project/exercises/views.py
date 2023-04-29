from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django.views.generic import DetailView
from django.views.generic import ListView
from store_project.exercises.models import Alternative
from store_project.exercises.models import Category
from store_project.exercises.models import Exercise


class ExerciseDetailView(DetailView):
    model = Exercise

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["yt_demo_id"] = self.object.get_yt_demo_id()
        context["yt_explan_id"] = self.object.get_yt_explan_id()
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
    template_name = "exercises/index.html"

    def get_queryset(self):
        self.category = get_object_or_404(Category, slug=self.kwargs["category"])
        return Exercise.objects.filter(categories=self.category)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        self.category = get_object_or_404(Category, slug=self.kwargs["category"])
        context["category"] = self.category.name
        context["categories"] = Category.objects.all()
        return context


@require_http_methods(["POST"])
def search(request):
    search = request.POST.get("search", "")
    category = request.POST.get("category", None)

    if category:
        exercises = Exercise.objects.filter(categories__name=category)
    else:
        exercises = Exercise.objects.all()

    if len(search) == 0:
        return render(
            request,
            "exercises/exercises.html",
            {
                "exercises": exercises.order_by("name"),
            },
        )

    return render(
        request,
        "exercises/exercises.html",
        {
            "exercises": exercises.filter(name__search=search),
        },
    )
