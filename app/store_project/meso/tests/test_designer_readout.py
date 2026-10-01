"""UAT-2 designer readout (#638, #645).

The periodization chart reads the block, and logged athlete lines collapse to
one summary.
"""

import pytest

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import Unit
from store_project.meso.serializers import serialize_mesocycle_grid

from ._helpers import day
from ._helpers import presc
from ._helpers import sub_line

pytestmark = pytest.mark.django_db


def _block(rows, *, day_name="Lower A", unit=Unit.POUNDS):
    """A 4-week block; ``rows`` = {exercise name: [week1..week4 cell text]}."""
    plan = PlanFactory(
        relationship=CoachAthleteFactory(), status=Plan.Status.ACTIVE, unit=unit
    )
    meso = MesocycleFactory(plan=plan, week_count=4)
    weeks = [WeekFactory(mesocycle=meso, index=i, phase="Accum") for i in range(1, 5)]
    sessions = [day(weeks[0], day_number=1, name=day_name, order=0)]
    sessions += [day(w, session_slot=sessions[0].session_slot) for w in weeks[1:]]
    cells = {}
    for order, (name, texts) in enumerate(rows.items()):
        first = presc(sessions[0], name=name, order=order, text=texts[0])
        cells[name] = [first]
        for week, text in zip(weeks[1:], texts[1:]):
            cells[name].append(
                presc(exercise_slot=first.exercise_slot, week=week, text=text)
            )
    return meso, weeks, cells


UAT_BLOCK = {
    "Back Squat": ["3x5 @ 225", "3x5 @ 235", "3x5 @ 245", "2x5 @ 185"],
    "Romanian Deadlift": ["3x8 @ 155"] * 4,
    "Bench Press": ["4x8 @ 155"] * 4,
    "Barbell Row": ["3x10"] * 4,
}


class TestPeriodizationReadout:
    def test_deload_week_drops_in_both_bars(self):
        meso, _, _ = _block(UAT_BLOCK)
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert wk[3]["vol"] < wk[2]["vol"]
        assert wk[3]["inten"] < wk[2]["inten"]

    def test_progression_rises_before_the_deload(self):
        meso, _, _ = _block(UAT_BLOCK)
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert wk[1]["inten"] > wk[0]["inten"]
        assert wk[2]["inten"] > wk[1]["inten"]
        assert max(w["vol"] for w in wk) == 100

    def test_exact_values_for_the_uat_block(self):
        meso, _, _ = _block(UAT_BLOCK)
        wk = serialize_mesocycle_grid(meso)["weeks"]
        # Volume: wk1-3 = 15+24+32+30 = 101, wk4 = 10+24+32+30 = 96.
        assert [w["vol"] for w in wk] == [100, 100, 100, 95]
        # Intensity: squat 225/245 .918, 235/245 .959, 1.0, 185/245 .755;
        # RDL and bench 1.0 every week; the row has no load.
        assert [w["inten"] for w in wk] == [97, 99, 100, 92]

    def test_nothing_parses_means_no_bars(self):
        meso, _, _ = _block({"Back Squat": ["work up", "heavy", "heavy", "easy"]})
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert all(w["vol"] is None and w["inten"] is None for w in wk)

    def test_volume_without_loads_has_no_intensity_bar(self):
        meso, _, _ = _block({"Pull-up": ["3x8", "3x9", "3x10", "2x8"]})
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert all(w["inten"] is None for w in wk)
        assert wk[3]["vol"] < wk[2]["vol"]

    def test_percent_loads_read_as_intensity(self):
        meso, _, _ = _block(
            {"Squat": ["3x5 @ 70%", "3x5 @ 75%", "3x5 @ 80%", "3x5 @ 60%"]}
        )
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert wk[3]["inten"] < wk[0]["inten"] < wk[2]["inten"]

    def test_a_skipped_cell_adds_nothing(self):
        meso, _, cells = _block({"Squat": ["3x5 @ 100"] * 4})
        cells["Squat"][3].skipped = True
        cells["Squat"][3].save()
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert wk[3]["vol"] is None and wk[3]["inten"] is None

    def test_stored_week_numbers_are_not_trusted(self):
        meso, weeks, _ = _block({"Squat": ["3x5 @ 100"] * 4})
        for w in weeks:
            w.volume, w.intensity = 70, 65
            w.save()
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert wk[0]["vol"] == 100 and wk[0]["inten"] == 100


class TestDaysCarryNames:
    def test_grid_days_carry_the_coach_given_name(self):
        meso, _, _ = _block(UAT_BLOCK, day_name="Lower A")
        assert serialize_mesocycle_grid(meso)["days"][0]["name"] == "Lower A"


class TestAthleteSummary:
    def _logged(self, unit_on_set):
        meso, weeks, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        log = SessionLogFactory(
            session=cell.exercise_slot.session_slot.sessions.first()
        )
        for n, (load, rpe) in enumerate([("205", "7"), ("225", "9"), ("215", "8")], 1):
            line = sub_line(cell, f"{load} x 5 @{rpe}", athlete_authored=True)
            LoggedSetFactory(
                session_log=log,
                prescription=cell,
                source_line=line,
                set_number=n,
                load=load,
                reps="5",
                rpe=rpe,
                unit=unit_on_set,
            )
        return meso, cell

    def test_three_logged_lines_summarise_to_the_top_set(self):
        meso, cell = self._logged(Unit.KILOGRAMS)
        data = serialize_mesocycle_grid(meso)
        cell_data = data["days"][0]["rows"][0]["cells"][str(cell.week_id)]
        # The unit stamped on the LoggedSet (kg) wins over the plan's (lb).
        assert cell_data["athlete_summary"] == {
            "sets": 3,
            "load": "225",
            "unit": "kg",
            "rpe": "9",
        }

    def test_a_cell_with_no_athlete_lines_has_no_summary(self):
        meso, _, _ = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cells = serialize_mesocycle_grid(meso)["days"][0]["rows"][0]["cells"]
        assert all(c["athlete_summary"] is None for c in cells.values())

    def test_lines_with_no_logged_set_fall_back_to_their_text(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        sub_line(cell, "225 x 5", athlete_authored=True)
        sub_line(cell, "felt heavy", athlete_authored=True)
        data = serialize_mesocycle_grid(meso)
        summary = data["days"][0]["rows"][0]["cells"][str(cell.week_id)][
            "athlete_summary"
        ]
        assert summary["sets"] == 2 and summary["load"] == "225"
        assert summary["unit"] == "lb"  # the plan's unit
