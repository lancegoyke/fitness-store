from django.views.generic import DetailView, ListView

from store_project.exercises.models import Alternative, Exercise


class ExerciseDetailView(DetailView):
    model = Exercise

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["alternatives"] = Alternative.objects.filter(original=self.get_object())
        return context


class ExerciseListView(ListView):
    model = Exercise
    context_object_name = "exercises"
