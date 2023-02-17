from typing import Dict, List
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import HttpResponseServerError
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.views import generic
from django.views.decorators.http import require_http_methods

import requests

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


class MealPlanBuilderView(generic.CreateView):
    model = models.Meal
    form_class = forms.MealForm
    template_name = "meals/meal_plan_builder.html"


class MealListView(generic.ListView):
    model = models.Meal
    form_class = forms.MealForm


class MealCreateView(PermissionRequiredMixin, generic.CreateView):
    model = models.Meal
    form_class = forms.MealForm
    template_name = "meals/meal_create.html"
    permission_required = "meals.add_meal"
    permission_denied_message = "You don't have permission to access that page"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["ingredients"] = models.Ingredient.objects.all()
        return context


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


# class IngredientCreateView(generic.CreateView):
#     model = models.Ingredient
#     form_class = forms.IngredientForm
#     template_name = "meals/ingredient_create.html"

class IngredientCreateView(generic.CreateView):
    model = models.Ingredient
    form_class = forms.IngredientForm
    success_url = reverse_lazy("meals:meal_create")


class IngredientDetailView(generic.DetailView):
    model = models.Ingredient
    form_class = forms.IngredientForm


class IngredientUpdateView(generic.UpdateView):
    model = models.Ingredient
    form_class = forms.IngredientForm
    pk_url_kwarg = "pk"


class IngredientDeleteView(generic.DeleteView):
    model = models.Ingredient
    success_url = reverse_lazy("meals:ingredient_list")


class IngredientListSearchView(generic.ListView):
    model = models.Ingredient
    template_name = "meals/ingredient_search.html"


@require_http_methods(["GET"])
def ingredient_search_local(request):
    search = request.GET.get("q")

    if len(search) > 0:
        ingredients = models.Ingredient.objects.filter(name__search=search)
    else:
        ingredients = models.Ingredient.objects.all().order_by("-created")

    return render(request, "meals/ingredients.html", {"ingredients": ingredients})


@require_http_methods(["GET"])
def ingredient_search_remote(request):
    search = request.GET.get("ingredient-search", "")

    # results come from Spoonacular nutrition database API
    endpoint = "https://api.spoonacular.com/food/ingredients/search"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": settings.SPOONACULAR_API_KEY,
    }
    params = {
        "query": search,
        "metaInformation": True,
    }
    r = requests.get(endpoint, headers=headers, params=params)
    results = r.json()["results"]

    context = {}
    if len(search) == 0:
        context["results"] = None
    else:
        context["results"] = results

    return render(request, "meals/ingredient_search_results.html", context)


@require_http_methods(["GET"])
def ingredient_amount_form(request):
    ingredient_id = request.GET.get("ingredient-id")
    name = request.GET.get("name")
    possible_units = request.GET.get("possible-units")[:-1].split(",")
    units = [(unit, unit) for unit in possible_units]
    form = forms.UnitAmountForm(
        ingredient_id=ingredient_id,
        name=name,
        units=units
    )

    context = {
        "form": form,
        "ingredient_id": ingredient_id,
        "name": name,
    }

    return render(request, "meals/ingredient_amount_form.html", context)


@require_http_methods(["GET"])
def ingredient_nutrition_lookup(request):
    form = forms.UnitAmountForm(request.GET, units=[(x, x) for x in request.GET["unit"]])
    if form.is_valid():
        ingredient_id = form.cleaned_data["ingredient_id"]
        name = form.cleaned_data["name"]
        amount = form.cleaned_data["amount"]
        unit = form.cleaned_data["unit"]
        
        endpoint = f"https://api.spoonacular.com/food/ingredients/{ingredient_id}/information"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": settings.SPOONACULAR_API_KEY,
        }
        params = {
            "amount": amount,
            "unit": unit,
        }
        r = requests.get(endpoint, headers=headers, params=params)
        nutrients: List[Dict] = r.json()["nutrition"]["nutrients"]
        
        desired = [
            "Calories",
            "Net Calories",
            "Fat",
            "Carbohydrates",
            "Fiber",
            "Net Carbohydrates",
            "Protein",
        ]

        # filter for only desired results
        nutrients = [n for n in nutrients if n["name"] in desired]
        cals = next(c for c in nutrients if c["name"] == "Calories")["amount"]
        fat = next(f for f in nutrients if f["name"] == "Fat")["amount"]
        carbs = next(c for c in nutrients if c["name"] == "Carbohydrates")["amount"]
        fiber = next(f for f in nutrients if f["name"] == "Fiber")["amount"]
        net_carbs = next(nc for nc in nutrients if nc["name"] == "Net Carbohydrates")["amount"]
        protein = next(p for p in nutrients if p["name"] == "Protein")["amount"]
        net_cals = cals - fiber * 4
        nutrients.append({
            "name": "Net Calories",
            "amount": net_cals,
            "unit": "kcal",
        })

        # add index to desired results
        for n in nutrients:
            n["index"] = desired.index(n["name"])

        # sort by index
        sorted_nutrients = sorted(nutrients, key=lambda n: n["index"])

        context = {
            "nutrients": sorted_nutrients,
            "ingredient_id": ingredient_id,
            "name": name,
            "amount": round(amount),
            "unit": unit,
            "cals": round(cals),
            "net_cals": round(net_cals),
            "fat": round(fat),
            "carbs": round(carbs),
            "fiber": round(fiber),
            "net_carbs": round(net_carbs),
            "protein": round(protein),
        }
            
        return render(request, "meals/nutrition_facts.html", context)

    else:
        return HttpResponseServerError("Form invalid")


@require_http_methods(["GET"])
def meal_meta_update_form(request):
    return render(request, "meals/htmx/meta_form.html", {})


@require_http_methods(["POST"])
def meal_meta_session(request):
    name = request.POST.get("name")
    description = request.POST.get("description")
    request.session["recipe_name"] = name
    request.session["recipe_description"] = description
    return render(
        request,
        "meals/htmx/meta.html",
        {
            "name": name,
            "description": description,
        }
    )
