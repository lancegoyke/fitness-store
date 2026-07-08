"""The ``Prescription`` cell's resolving identity, esp. one-week swap semantics.

A cell inherits identity from its ``ExerciseSlot`` unless a one-week swap is set.
A swap replaces identity WHOLESALE — a free-text swap (``swap_name`` only) of a
catalog-backed row must not keep resolving to the original catalog exercise, or
logs/1RM keying and catalog-linked checks would mis-attribute the substituted week.
"""

import pytest

from store_project.exercises.factories import ExerciseFactory
from store_project.meso import serializers
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


def _cell_on_catalog_slot():
    """A cell whose slot is catalog-backed (``exercise`` set), named 'Back Squat'."""
    plan = PlanFactory()
    week = WeekFactory(mesocycle__plan=plan, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    catalog = ExerciseFactory()
    return presc(session, name="Back Squat", exercise=catalog), catalog


class TestCellSwapIdentity:
    def test_no_swap_uses_the_slot_identity(self):
        cell, catalog = _cell_on_catalog_slot()
        assert cell.name == "Back Squat"
        assert cell.exercise_id == catalog.pk
        assert cell.is_catalog_linked is True

    def test_free_text_swap_drops_the_slots_catalog_link(self):
        # A one-week free-text swap of a catalog-backed row is NOT the original
        # exercise — it must not fall back to the slot's catalog identity.
        cell, catalog = _cell_on_catalog_slot()
        assert cell.exercise_id == catalog.pk  # baseline

        cell.swap_name = "Goblet Squat"
        cell.save(update_fields=["swap_name"])

        assert cell.name == "Goblet Squat"
        assert cell.exercise is None
        assert cell.exercise_id is None
        assert cell.is_catalog_linked is False

    def test_catalog_swap_resolves_to_the_swap_exercise(self):
        cell, _ = _cell_on_catalog_slot()
        substitute = ExerciseFactory()

        cell.swap_exercise = substitute
        cell.save(update_fields=["swap_exercise"])

        assert cell.exercise_id == substitute.pk
        assert cell.name == substitute.name
        assert cell.is_catalog_linked is True


class TestSkippedCellsAreNotTrainable:
    """A one-week ``skipped`` cell must not render as a loggable blank row.

    "Not trained this week" is hidden from the P0 week-at-a-time surfaces (the P1
    multi-week table renders it as an em-dash instead), while structure-preserving
    logic (group sync, snapshots) still sees every cell via ``cells()``.
    """

    def _day_with_a_skip(self):
        plan = PlanFactory()
        week = WeekFactory(mesocycle__plan=plan, index=1, is_current=True)
        session = day(week, day_number=1, name="Lower")
        presc(session, name="Squat")
        presc(session, name="Skipped Curl", skipped=True)
        return session

    def test_trainable_cells_excludes_skipped_but_cells_keeps_it(self):
        session = self._day_with_a_skip()
        assert session.cells().count() == 2
        assert session.trainable_cells().count() == 1

    def test_serialize_session_omits_skipped_rows(self):
        session = self._day_with_a_skip()
        data = serializers.serialize_session(session)
        assert [e["name"] for e in data["exercises"]] == ["Squat"]
        assert all(e["skipped"] is False for e in data["exercises"])
