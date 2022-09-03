from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render

from store_project.tracking.models import Test


def test_list(request):
    context = {
        "tests": Test.objects.all(),
    }
    return render(request, "tracking/test_list.html", context)


@login_required
def test_detail(request, pk):
    test = Test.objects.get(pk=pk)
    user = request.user
    if user.is_staff:
        test_results = test.measurement_type.model_class().objects.filter(test=test)
    else:
        test_results = test.measurement_type.model_class().objects.filter(test=test, user=user)

    context = {
        "test": test,
        "test_results": test_results,
    }
    return render(request, "tracking/test_detail.html", context)


@login_required
def test_result_create(request, pk):
    """
    We should filter results based on the viewer
      1) Athletes should only add their own scores
      2) Coaches should be able to see all users

    The test is given, so should be hidden from user.
    """
    test = Test.objects.get(pk=pk)

    if request.method == "POST":
        if request.user.is_staff:
            form = test.get_measure_staff_form_cls()(request.POST)
            user = None
        else:
            form = test.get_measure_athlete_form_cls()(request.POST)
            user = request.user  # the logged in  user

        if form.is_valid():
            measure = form.save(commit=False)
            measure.test = test
            if user:
                measure.user = user
            measure.save()
            return redirect(test)

    if request.user.is_staff:
        form = test.get_measure_staff_form_cls()()
    else:
        form = test.get_measure_athlete_form_cls()()

    context = {
        "test": test,
        "form": form,
    }
    return render(request, "tracking/test_result_create.html", context)


@login_required
def test_result_bulk(request, pk):
    """Allows a coach to bulk add results for a single test"""
    test = Test.objects.get(pk=pk)
    if not request.user.is_staff:
        messages.error("Only coaches have access to bulk add test results")
        return redirect(test)

    test_results = test.measurement_type.model_class().objects.filter(test=test)

    # FORMSET
    # if request.method == "POST":
    #     formset = test.get_measure_test_bulk_form_cls()(request.POST)
    #     if formset.is_valid():
    #         formset.instance = test
    #         formset.save()
    #         return redirect(test)

    # formset = test.get_measure_test_bulk_form_cls()()

    # FORM
    form = test.get_measure_staff_form_cls()(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            result = form.save(commit=False)
            result.test = test
            result.save()
            return render(
                request,
                "tracking/partials/result_row.html",
                context={"result": result},
            )
        else:
            return render(
                request,
                "tracking/partials/result_form.html",
                context={"form": form},
            )

    context = {
        "test": test,
        "test_results": test_results,
        # "formset": formset,
        "form": form,
    }
    return render(request, "tracking/test_result_bulk_form.html", context)


def result_create_form(request, pk):
    test = Test.objects.get(pk=pk)
    form = test.get_measure_staff_form_cls()()

    context = {
        "test": test,
        "form": form,
    }
    return render(
        request,
        "tracking/partials/result_form.html",
        context,
    )


def create_result(request, pk):
    test = Test.objects.get(pk=pk)
    form = test.get_measure_staff_form_cls()(request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            result = form.save(commit=False)
            result.test = test
            result.save()
            return HttpResponse("success")
        else:
            return render(request, "tracking/partials/result_form.html", context={"form": form})

    context = {
        "test": test,
        "form": form,
    }

    return render(request, "tracking/result_create.html", context)
