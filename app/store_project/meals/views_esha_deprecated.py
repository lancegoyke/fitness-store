@require_http_methods(["POST"])
def ingredient_search(request):
    search = request.POST.get("ingredient-search", "")

    # results come from ESHA nutrition database API
    endpoint = "https://nutrition-api.esha.com/foods"
    headers = {
        "Accept": "application/json",
        "Ocp-Apim-Subscription-Key": settings.ESHA_SUB_KEY,
    }
    params = {
        "query": search,
        "start": 0,
        "count": 25,
        "spell": True,
    }
    r = requests.get(endpoint, headers=headers, params=params)
    results = r.json()["items"]

    if len(search) == 0:
        return render(request, "meals/ingredients.html", {"results": None})

    return render(request, "meals/ingredients.html", {"results": results})


@require_http_methods(["GET", "POST"])
def ingredient_nutrition(request):
    results = None
    if request.method == "POST":
        form = forms.UnitAmountForm(request.POST)
        if form.is_valid():
            ingredient_id = request.POST.get("ingredient-id")
            amount = form.cleaned_data["amount"]
            unit = form.cleaned_data["unit"]
            
            # results come from ESHA nutrition database API
            endpoint = "https://nutrition-api.esha.com/analysis"
            headers = {
                "Accept": "application/json",
                "Ocp-Apim-Subscription-Key": settings.ESHA_SUB_KEY,
            }
            payload = {
                "items": [
                    {
                        "id": ingredient_id,
                        "quantity": amount,
                        "unit": models.Unit.objects.get(abbr=unit).id.urn,
                    },
                ]
            }
            r = requests.post(endpoint, headers=headers, json=payload)
            results = r.json()["results"]  # there should only be one result
            return render(request, "meals/nutrition_facts.html", {"results": results})

    else:
        form = forms.UnitAmountForm()
        ingredient_id = request.GET.get("ingredient-id")

    context = {
        "form": form,
        "ingredient_id": ingredient_id,
        "results": results,
    }

    return render(request, "meals/ingredient_nutrition.html", context)
