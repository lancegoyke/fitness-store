from django.contrib import messages
from django.shortcuts import render

from store_project.meals.forms import MacroForm


def macro_calculator(request):
    """Allow user to calculate daily nutrition requirements"""

    if request.method == "POST":
        form = MacroForm(request.POST)
        if form.is_valid():
            messages.success(request, "Success!")
    else:
        form = MacroForm()

    context = {
        "form": form,
    }

    return render(request, "meals/macro_calculator.html", context)
