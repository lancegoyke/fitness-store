from decimal import Decimal

from store_project.meals.forms import MacroForm


class Macros:
    def __init__(
        self, weight, weight_unit, height, height_unit, age, sex, activity_level, goal
    ):
        self.weight = weight
        self.weight_unit = weight_unit
        self.height = height
        self.height_unit = height_unit
        self.age = age
        self.sex = sex
        self.activity_level = activity_level
        self.goal = goal

    def kcals(self):
        """The Mifflin-St. Jeor equation"""

        # base
        if self.weight_unit == MacroForm.WEIGHT_METRIC:
            weight_kg = self.weight
        elif self.weight_unit == MacroForm.WEIGHT_IMPERIAL:
            weight_kg = self.weight / 2.2046  # convert lbs to kg
        else:
            raise Exception("Could not find an appropriate weight unit")

        if self.height_unit == MacroForm.HEIGHT_METRIC:
            height_cm = self.height
        elif self.height_unit == MacroForm.HEIGHT_IMPERIAL:
            height_cm = self.height * 2.54  # convert in to cm
        else:
            raise Exception("Could not find a height unit")

        kcals = (10 * weight_kg) + (6.25 * height_cm) - (5 * self.age)

        # sex modification
        if self.sex == MacroForm.SEX_M:
            kcals += 5
        elif self.sex == MacroForm.SEX_F:
            kcals -= 161
        else:
            raise Exception("Could not find a hormonal sex")

        # activity
        if self.activity_level == MacroForm.SEDENTARY:
            kcals *= 1.1
        elif self.activity_level == MacroForm.LOWACTIVE:
            kcals *= 1.375
        elif self.activity_level == MacroForm.ACTIVE:
            kcals *= 1.65
        elif self.activity_level == MacroForm.HIGHACTIVE:
            kcals *= 1.9
        else:
            raise Exception("Could not find an activity level")

        # goal
        if self.goal == MacroForm.MAINTENANCE:
            pass
        elif self.goal == MacroForm.FAT_LOSS:
            kcals -= 500
        elif self.goal == MacroForm.MUSCLE_GAIN:
            kcals += 500
        else:
            raise Exception("Could not find a goal")

        return kcals

    def carbs(self):
        """Remainder of calories"""

        carb_kcals = self.kcals() - (self.protein() * 4) - (self.fat() * 9)
        return carb_kcals / 4  # 4 kcal/g of carb

    def fat(self):
        """One-third of total calories"""

        return self.kcals() / 3 / 9  # one-third of kcals, 9 kcal/g of fat

    def protein(self):
        """1 gram of protein per pound of bodyweight or 2.2g/kg"""

        if self.weight_unit == MacroForm.WEIGHT_METRIC:
            return self.weight * 2.2
        elif self.weight_unit == MacroForm.WEIGHT_IMPERIAL:
            return self.weight * 1.0
        else:
            raise Exception("Could not find a weight unit")
