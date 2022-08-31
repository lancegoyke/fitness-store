from django.shortcuts import render

from store_project.tracking.models import Test


def test_list(request):
    context = {
        "tests": Test.objects.all(),
    }
    return render(request, "tracking/test_list.html", context)


def test_detail(request, pk):
    test = Test.objects.get(pk=pk)
    context = {
        "test": test,
        "test_results": test.measurement_type.model_class().objects.all(),
    }
    return render(request, "tracking/test_detail.html", context)


def test_result_create(request, pk):
    test = Test.objects.get(pk=pk)
    context = {
        "test": test,
        "form": test.get_measurement_form(),
    }
    return render(request, "tracking/test_detail_add_result.html", context)
