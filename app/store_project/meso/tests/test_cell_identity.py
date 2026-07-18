"""The ``Prescription`` cell's resolving identity (text-first, Phase 2a).

A cell always inherits identity from its block-shared ``ExerciseSlot`` — the
one-week ``swap_*`` override is gone. A substitution is freeform sub-line text
now (plan §2.6), which never changes the row's resolving identity: logs/1RM
keying and catalog-linked checks stay on the slot for every week.
"""

import pytest

from store_project.exercises.factories import ExerciseFactory
from store_project.meso import serializers
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory

from ._helpers import day
from ._helpers import presc
from ._helpers import sub_line

pytestmark = pytest.mark.django_db


def _cell_on_catalog_slot():
    """A cell whose slot is catalog-backed (``exercise`` set), named 'Back Squat'."""
    plan = PlanFactory()
    week = WeekFactory(mesocycle__plan=plan, index=1)
    session = day(week, day_number=1, name="Lower")
    catalog = ExerciseFactory()
    return presc(session, name="Back Squat", exercise=catalog), catalog


class TestCellIdentityDelegatesToSlot:
    def test_cell_uses_the_slot_identity(self):
        cell, catalog = _cell_on_catalog_slot()
        assert cell.name == "Back Squat"
        assert cell.exercise_id == catalog.pk
        assert cell.is_catalog_linked is True

    def test_slot_rename_resolves_through_the_cell(self):
        # Identity is block-wide: renaming the slot renames every week's cell.
        cell, catalog = _cell_on_catalog_slot()
        cell.exercise_slot.name = "Front Squat"
        cell.exercise_slot.save(update_fields=["name"])

        cell.refresh_from_db()
        assert cell.name == "Front Squat"
        assert cell.exercise_id == catalog.pk  # catalog link untouched

    def test_a_substitution_sub_line_leaves_identity_alone(self):
        # Phase 2a: a substitution is freeform sub-line text, never an identity
        # override — the row (and even the sub-line cell itself) still resolves
        # to the slot, so logs/1RM keying can't mis-attribute the week.
        cell, catalog = _cell_on_catalog_slot()
        sub = sub_line(cell, "Goblet Squat")

        assert cell.name == "Back Squat"
        assert cell.exercise_id == catalog.pk
        assert cell.is_catalog_linked is True
        assert sub.name == "Back Squat"
        assert sub.exercise_id == catalog.pk


class TestSkippedCellsAreNotTrainable:
    """A one-week ``skipped`` cell must not render as a loggable blank row.

    "Not trained this week" is hidden from the P0 week-at-a-time surfaces (the P1
    multi-week table renders it as an em-dash instead), while structure-preserving
    logic (group sync, snapshots) still sees every cell via ``cells()``.
    """

    def _day_with_a_skip(self):
        plan = PlanFactory()
        week = WeekFactory(mesocycle__plan=plan, index=1)
        session = day(week, day_number=1, name="Lower")
        presc(session, name="Squat")
        presc(session, name="Skipped Curl", skipped=True)
        return session

    def test_trainable_cells_excludes_skipped_but_cells_keeps_it(self):
        session = self._day_with_a_skip()
        assert session.cells().count() == 2
        assert session.trainable_cells().count() == 1

    def test_serialize_session_keeps_skipped_with_a_flag_for_the_coach(self):
        # The coach designer shows the whole block lineup (the P1 table em-dashes a
        # skip); it's the athlete surfaces that drop skipped cells (trainable_cells).
        session = self._day_with_a_skip()
        data = serializers.serialize_session(session)
        assert [e["name"] for e in data["exercises"]] == ["Squat", "Skipped Curl"]
        skipped_row = next(e for e in data["exercises"] if e["name"] == "Skipped Curl")
        assert skipped_row["skipped"] is True
