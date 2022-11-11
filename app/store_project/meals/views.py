from django.contrib import messages
from django.shortcuts import render
from django.urls import reverse_lazy
from django.views import generic

from store_project.meals import models
from store_project.meals import forms
from store_project.meals.forms import MacroForm
from store_project.meals.macros import Macros


def macro_calculator(request):
    """Calculate daily nutrition requirements"""

    if request.method == "POST":
        form = MacroForm(request.POST)
        if form.is_valid():
            macros = Macros(
                weight=form.cleaned_data.get("weight"),
                weight_unit=form.cleaned_data.get("weight_unit"),
                height=form.cleaned_data.get("height"),
                height_unit=form.cleaned_data.get("height_unit"),
                age=form.cleaned_data.get("age"),
                sex=form.cleaned_data.get("sex"),
                activity_level=form.cleaned_data.get("activity_level"),
                goal=form.cleaned_data.get("goal"),
            )
            messages.success(
                request,
                f"Success! Daily requirements: kcals = {macros.kcals():.0f}, protein = {macros.protein():.0f}, fat = {macros.fat():.0f}, carbs = {macros.carbs():.0f}",
            )
    else:
        form = MacroForm()

    context = {
        "form": form,
    }

    return render(request, "meals/macro_calculator.html", context)


class MealListView(generic.ListView):
    model = models.Meal
    form_class = forms.MealForm


class MealCreateView(generic.CreateView):
    model = models.Meal
    form_class = forms.MealForm


class MealDetailView(generic.DetailView):
    model = models.Meal
    form_class = forms.MealForm


class MealUpdateView(generic.UpdateView):
    model = models.Meal
    form_class = forms.MealForm
    pk_url_kwarg = "pk"


class MealDeleteView(generic.DeleteView):
    model = models.Meal
    success_url = reverse_lazy("meals:meal_list")


class IngredientListView(generic.ListView):
    model = models.Ingredient
    form_class = forms.IngredientForm


class IngredientCreateView(generic.CreateView):
    model = models.Ingredient
    form_class = forms.IngredientForm


class IngredientDetailView(generic.DetailView):
    model = models.Ingredient
    form_class = forms.IngredientForm


class IngredientUpdateView(generic.UpdateView):
    model = models.Ingredient
    form_class = forms.IngredientForm
    pk_url_kwarg = "pk"


class IngredientDeleteView(generic.DeleteView):
    model = models.Ingredient
    success_url = reverse_lazy("meals_Ingredient_list")
