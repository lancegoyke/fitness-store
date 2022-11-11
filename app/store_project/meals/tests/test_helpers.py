import random
import string

from django.contrib.auth.models import User
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import AbstractBaseUser
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType

from store_project.meals import models as meals_models


def random_string(length=10):
    # Create a random string of length length
    letters = string.ascii_lowercase
    return "".join(random.choice(letters) for i in range(length))


def create_User(**kwargs):
    defaults = {
        "username": "%s_username" % random_string(5),
        "email": "%s_username@tempurl.com" % random_string(5),
    }
    defaults.update(**kwargs)
    return User.objects.create(**defaults)


def create_AbstractUser(**kwargs):
    defaults = {
        "username": "%s_username" % random_string(5),
        "email": "%s_username@tempurl.com" % random_string(5),
    }
    defaults.update(**kwargs)
    return AbstractUser.objects.create(**defaults)


def create_AbstractBaseUser(**kwargs):
    defaults = {
        "username": "%s_username" % random_string(5),
        "email": "%s_username@tempurl.com" % random_string(5),
    }
    defaults.update(**kwargs)
    return AbstractBaseUser.objects.create(**defaults)


def create_Group(**kwargs):
    defaults = {
        "name": "%s_group" % random_string(5),
    }
    defaults.update(**kwargs)
    return Group.objects.create(**defaults)


def create_ContentType(**kwargs):
    defaults = {
    }
    defaults.update(**kwargs)
    return ContentType.objects.create(**defaults)


def create_meals_meal(**kwargs):
    defaults = {}
    defaults["ingredients"] = ""
    defaults["net_cals"] = ""
    defaults["description"] = ""
    defaults["fat"] = ""
    defaults["cals"] = ""
    defaults["carbs"] = ""
    defaults["net_carbs"] = ""
    defaults["fiber"] = ""
    defaults["protein"] = ""
    defaults.update(**kwargs)
    return meals_models.Meal.objects.create(**defaults)


def create_meals_ingredient(**kwargs):
    defaults = {}
    defaults["fiber"] = ""
    defaults["carbs"] = ""
    defaults["amount"] = ""
    defaults["fat"] = ""
    defaults["name"] = ""
    defaults["net_carbs"] = ""
    defaults["cals"] = ""
    defaults["unit"] = ""
    defaults["net_cals"] = ""
    defaults["protein"] = ""
    defaults.update(**kwargs)
    return meals_models.Ingredient.objects.create(**defaults)
