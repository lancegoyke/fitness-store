import pytest
import test_helpers

from django.urls import reverse


pytestmark = [pytest.mark.django_db]


def tests_meal_list_view(client):
    instance1 = test_helpers.create_meals_meal()
    instance2 = test_helpers.create_meals_meal()
    url = reverse("meals_meal_list")
    response = client.get(url)
    assert response.status_code == 200
    assert str(instance1) in response.content.decode("utf-8")
    assert str(instance2) in response.content.decode("utf-8")


def tests_meal_create_view(client):
    url = reverse("meals:meal_create")
    data = {
        "ingredients": "text",
        "net_cals": 1,
        "description": "text",
        "fat": 1,
        "cals": 1,
        "carbs": 1,
        "net_carbs": 1,
        "fiber": 1,
        "protein": 1,
    }
    response = client.post(url, data)
    assert response.status_code == 302


def tests_meal_detail_view(client):
    instance = test_helpers.create_meals_meal()
    url = reverse("meals:meal_detail", args=[instance.pk, ])
    response = client.get(url)
    assert response.status_code == 200
    assert str(instance) in response.content.decode("utf-8")


def tests_meal_update_view(client):
    instance = test_helpers.create_meals_meal()
    url = reverse("meals:meal_update", args=[instance.pk, ])
    data = {
        "ingredients": "text",
        "net_cals": 1,
        "description": "text",
        "fat": 1,
        "cals": 1,
        "carbs": 1,
        "net_carbs": 1,
        "fiber": 1,
        "protein": 1,
    }
    response = client.post(url, data)
    assert response.status_code == 302


def tests_ingredient_list_view(client):
    instance1 = test_helpers.create_meals_ingredient()
    instance2 = test_helpers.create_meals_ingredient()
    url = reverse("meals:ingredient_list")
    response = client.get(url)
    assert response.status_code == 200
    assert str(instance1) in response.content.decode("utf-8")
    assert str(instance2) in response.content.decode("utf-8")


def tests_ingredient_create_view(client):
    url = reverse("meals:ingredient_create")
    data = {
        "fiber": 1,
        "carbs": 1,
        "amount": 1,
        "fat": 1,
        "name": "text",
        "net_carbs": 1,
        "cals": 1,
        "unit": "text",
        "net_cals": 1,
        "protein": 1,
    }
    response = client.post(url, data)
    assert response.status_code == 302


def tests_ingredient_detail_view(client):
    instance = test_helpers.create_meals_ingredient()
    url = reverse("meals:ingredient_detail", args=[instance.pk, ])
    response = client.get(url)
    assert response.status_code == 200
    assert str(instance) in response.content.decode("utf-8")


def tests_ingredient_update_view(client):
    instance = test_helpers.create_meals_ingredient()
    url = reverse("meals:ingredient_update", args=[instance.pk, ])
    data = {
        "fiber": 1,
        "carbs": 1,
        "amount": 1,
        "fat": 1,
        "name": "text",
        "net_carbs": 1,
        "cals": 1,
        "unit": "text",
        "net_cals": 1,
        "protein": 1,
    }
    response = client.post(url, data)
    assert response.status_code == 302
