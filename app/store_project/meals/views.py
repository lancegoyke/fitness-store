from django.contrib import messages
from django.shortcuts import render

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
