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


def _log(cell, **kwargs):
    """A SessionLog for ``cell``'s session, owned by the plan's athlete."""
    session = cell.exercise_slot.session_slot.sessions.get(week=cell.week)
    athlete = cell.week.mesocycle.plan.athlete
    return SessionLogFactory(session=session, athlete=athlete, **kwargs)


def _summary(meso, cell):
    data = serialize_mesocycle_grid(meso)
    row = next(
        r
        for d in data["days"]
        for r in d["rows"]
        if r["exercise_slot_id"] == cell.exercise_slot_id
    )
    return row["cells"][str(cell.week_id)]["athlete_summary"]


def _logged_set(cell, log, load, *, unit, rpe="8", n=1):
    line = sub_line(cell, f"{load} x 5 @{rpe}", athlete_authored=True)
    return LoggedSetFactory(
        session_log=log,
        prescription=cell,
        source_line=line,
        set_number=n,
        load=load,
        reps="5",
        rpe=rpe,
        unit=unit,
    )


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
        log = _log(cell)
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
            "missed": 0,
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

    def test_a_typed_unit_suffix_is_not_doubled(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        line = sub_line(cell, "225 lb x 5", athlete_authored=True)
        log = _log(cell)
        LoggedSetFactory(
            session_log=log,
            prescription=cell,
            source_line=line,
            load="225lb",
            unit="lb",
        )
        data = serialize_mesocycle_grid(meso)
        summary = data["days"][0]["rows"][0]["cells"][str(cell.week_id)][
            "athlete_summary"
        ]
        assert (summary["load"], summary["unit"]) == ("225", "lb")


class TestAthleteSummaryUnitsAndScope:
    def test_mixed_units_compare_by_kg_equivalent(self):
        # 150 lb is ~68 kg, so the 100 kg set is the heavier one.
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        log = _log(cell)
        _logged_set(cell, log, "100", unit=Unit.KILOGRAMS, n=1)
        _logged_set(cell, log, "150", unit=Unit.POUNDS, n=2)
        summary = _summary(meso, cell)
        assert (summary["load"], summary["unit"]) == ("100", "kg")

    def test_bodyweight_top_set(self):
        meso, _, cells = _block({"Pull-up": ["3x8"] * 4})
        cell = cells["Pull-up"][0]
        _logged_set(cell, _log(cell), "BW", unit="", rpe="8")
        assert _summary(meso, cell) == {
            "sets": 1,
            "load": "BW",
            "unit": "",
            "rpe": "8",
            "missed": 1,  # the helper logs 5 reps against a prescribed 8
        }

    def test_a_numeric_set_beats_a_bw_set(self):
        meso, _, cells = _block({"Pull-up": ["3x8"] * 4})
        cell = cells["Pull-up"][0]
        log = _log(cell)
        _logged_set(cell, log, "bw", unit="", rpe="6", n=1)
        _logged_set(cell, log, "10", unit=Unit.POUNDS, rpe="9", n=2)
        summary = _summary(meso, cell)
        assert (summary["load"], summary["rpe"]) == ("10", "9")

    def test_only_the_newest_log_counts(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        old = _log(cell)
        _logged_set(cell, old, "315", unit=Unit.POUNDS, n=1)
        new = _log(cell)
        _logged_set(cell, new, "225", unit=Unit.POUNDS, n=1)
        assert _summary(meso, cell)["load"] == "225"

    def test_an_older_logs_line_text_is_not_reparsed_as_a_fallback(self):
        # The old log's set is filtered out; its line must not come back via
        # the text fallback and beat the newest log's top set.
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        old_set = _logged_set(cell, _log(cell), "315", unit=Unit.POUNDS, n=1)
        old_set.source_line.text = "315lb x5"
        old_set.source_line.save()
        _logged_set(cell, _log(cell), "225", unit=Unit.POUNDS, n=1)
        assert _summary(meso, cell)["load"] == "225"

    def test_a_log_of_another_athlete_is_ignored(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        other = SessionLogFactory(
            session=cell.exercise_slot.session_slot.sessions.get(week=cell.week)
        )
        _logged_set(cell, other, "315", unit=Unit.POUNDS)
        sub_line(cell, "135 x 5", athlete_authored=True)
        # No log of the plan's athlete: the text fallback applies.
        assert _summary(meso, cell)["load"] == "135"

    def test_stamped_unit_beats_a_typed_suffix(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        _logged_set(cell, _log(cell), "225lb", unit=Unit.KILOGRAMS)
        summary = _summary(meso, cell)
        assert (summary["load"], summary["unit"]) == ("225", "kg")

    def test_text_fallback_suffix_is_that_sets_unit(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        sub_line(cell, "100kg x 5", athlete_authored=True)
        summary = _summary(meso, cell)
        assert (summary["load"], summary["unit"]) == ("100", "kg")


class TestAthleteSummaryPctAndReclaimed:
    def _pct_and_abs(self, order):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        log = _log(cell)
        sets = {
            "abs": lambda n: _logged_set(cell, log, "50", unit=Unit.KILOGRAMS, n=n),
            "pct": lambda n: _logged_set(cell, log, "90%", unit="", n=n),
        }
        for n, kind in enumerate(order, start=1):
            sets[kind](n)
        return _summary(meso, cell)

    def test_an_absolute_set_beats_a_pct_set_pct_first(self):
        summary = self._pct_and_abs(["pct", "abs"])
        assert (summary["load"], summary["unit"]) == ("50", "kg")

    def test_an_absolute_set_beats_a_pct_set_abs_first(self):
        summary = self._pct_and_abs(["abs", "pct"])
        assert (summary["load"], summary["unit"]) == ("50", "kg")

    def test_a_pct_only_log_picks_the_top_pct(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        log = _log(cell)
        _logged_set(cell, log, "90%", unit="", n=1)
        _logged_set(cell, log, "80%", unit="", n=2)
        summary = _summary(meso, cell)
        # The frontend renders ``load`` + optional `` unit``: "90%", no unit.
        assert (summary["load"], summary["unit"]) == ("90%", "")

    def test_an_older_logs_reclaimed_set_neither_wins_nor_falls_back(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        old = _log(cell)
        old_line = sub_line(cell, "300 x 5", athlete_authored=True)
        LoggedSetFactory(
            session_log=old,
            prescription=cell,
            source_line=None,
            reclaimed_line=old_line,
            set_number=1,
            load="300",
            reps="5",
            unit=Unit.POUNDS,
        )
        new = _log(cell)
        _logged_set(cell, new, "100", unit=Unit.POUNDS, n=1)
        assert _summary(meso, cell)["load"] == "100"

    def test_an_older_logs_blank_load_reclaimed_set_still_blocks_the_fallback(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        old = _log(cell)
        old_line = sub_line(cell, "315 x 5", athlete_authored=True)
        LoggedSetFactory(
            session_log=old,
            prescription=cell,
            source_line=None,
            reclaimed_line=old_line,
            set_number=1,
            load="",
            reps="5",
            unit=Unit.POUNDS,
        )
        new = _log(cell)
        _logged_set(cell, new, "225", unit=Unit.POUNDS, n=1)
        assert _summary(meso, cell)["load"] == "225"

    def test_a_reclaimed_set_of_the_newest_log_is_used(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        line = sub_line(cell, "100 x 5", athlete_authored=True)
        LoggedSetFactory(
            session_log=_log(cell),
            prescription=cell,
            source_line=None,
            reclaimed_line=line,
            set_number=1,
            load="120",
            reps="5",
            unit=Unit.POUNDS,
        )
        # The text says 100; the set standing behind the line says 120.
        assert _summary(meso, cell)["load"] == "120"


class TestSkippedLiftReadsLower:
    BLOCK = {
        "Squat": ["3x5 @ 200"] * 4,
        "Bench": ["3x5 @ 100", "3x5 @ 100", "skip", "3x5 @ 100"],
    }

    def test_skipping_a_lift_pulls_the_week_down(self):
        meso, _, _ = _block(self.BLOCK)
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert wk[1]["inten"] == 100
        assert wk[2]["inten"] == 50

    def test_a_skipped_flag_and_a_blank_cell_count_as_absent(self):
        meso, _, cells = _block(
            {"Squat": ["3x5 @ 200"] * 4, "Bench": ["3x5 @ 100"] * 4}
        )
        cells["Bench"][1].skipped = True
        cells["Bench"][1].save()
        cells["Bench"][2].text = "  "
        cells["Bench"][2].save()
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert [w["inten"] for w in wk] == [100, 50, 50, 100]

    def test_a_present_rpe_only_week_does_not_pull_intensity_down(self):
        # Guard: present-but-unloaded is excluded from the mean, not a zero.
        meso, _, _ = _block(
            {
                "Squat": ["3x5 @ 200"] * 4,
                "Bench": ["3x5 @ 100", "3x5 @ 100", "3x5 @ RPE 8", "3x5 @ 100"],
            }
        )
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert wk[2]["inten"] == 100


class TestWeekReadoutsMixedUnits:
    def test_kg_and_lb_tokens_compare_in_kg_equivalents(self):
        # 225 lb = 102.06 kg vs 100 kg: week 1 is 100/102.06, week 2 is 1.0.
        meso, _, _ = _block({"Squat": ["3x5 @ 100kg", "3x5 @ 225lb", "3x5", "3x5"]})
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert wk[0]["inten"] == 98
        assert wk[1]["inten"] == 100

    def test_bw_rpe_is_not_read_as_a_load(self):
        meso, _, _ = _block({"Dip": ["3x8 BW @8"] * 4})
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert all(w["inten"] is None for w in wk)


class TestLoadTokenEdges:
    def test_kilos_suffix_is_kilograms_on_a_pound_plan(self):
        # 100 kilos = 220 lb beats 150 lb: week 1 is the heaviest.
        meso, _, _ = _block({"Squat": ["3x5 @ 100kilos", "3x5 @ 150lb", "3x5", "3x5"]})
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert (wk[0]["inten"], wk[1]["inten"]) == (100, 68)

    def test_an_absurdly_large_load_does_not_crash_the_readout(self):
        meso, _, _ = _block({"Squat": ["3x5 @ " + "9" * 400] * 4})
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert all(w["inten"] is None for w in wk)

    def test_weighted_bodyweight_load_still_counts_when_the_lift_is_skipped(self):
        meso, _, _ = _block(
            {
                "Squat": ["3x5 @ 40", "3x5 @ 80", "", ""],
                "Pull-up": ["3x8 BW @ 20kg", "skip", "", ""],
            },
            unit=Unit.KILOGRAMS,
        )
        wk = serialize_mesocycle_grid(meso)["weeks"]
        assert (wk[0]["inten"], wk[1]["inten"]) == (75, 50)


class TestAthleteSummaryRpeAndMisses:
    """#688.2: the marker shows the highest RPE and how many sets missed reps."""

    def _sets(self, text, sets):
        """``sets`` = [(load, reps, rpe)] logged against a cell prescribed ``text``."""
        meso, _, cells = _block({"Back Squat": [text] * 4})
        cell = cells["Back Squat"][0]
        log = _log(cell)
        for n, (load, reps, rpe) in enumerate(sets, 1):
            line = sub_line(cell, f"{load} x {reps} @{rpe}", athlete_authored=True)
            LoggedSetFactory(
                session_log=log,
                prescription=cell,
                source_line=line,
                set_number=n,
                load=load,
                reps=reps,
                rpe=rpe,
                unit=Unit.POUNDS,
            )
        return _summary(meso, cell)

    def test_highest_rpe_across_sets_and_one_miss(self):
        # The top-load set (225) has RPE 8; the 9.5 is on a lighter, short set.
        summary = self._sets(
            "3x5 @ 225", [("225", "5", "8"), ("215", "5", "8.5"), ("205", "4", "9.5")]
        )
        assert summary["rpe"] == "9.5"
        assert summary["missed"] == 1
        assert summary["load"] == "225"

    def test_no_miss_is_zero(self):
        summary = self._sets("3x5 @ 225", [("225", "5", "8"), ("225", "6", "8")])
        assert summary["missed"] == 0

    def test_every_short_set_counts(self):
        summary = self._sets("3x5 @ 225", [("225", "4", "8"), ("225", "3", "9")])
        assert summary["missed"] == 2

    @pytest.mark.parametrize("text", ["3x8-10 @ 225", "3xAMRAP @ 225"])
    def test_non_numeric_prescribed_reps_never_miss(self, text):
        summary = self._sets(text, [("225", "3", "8")])
        assert summary["missed"] == 0

    def test_text_fallback_counts_a_short_set(self):
        meso, _, cells = _block({"Back Squat": ["3x5 @ 225"] * 4})
        cell = cells["Back Squat"][0]
        sub_line(cell, "225 x 3, RPE 9", athlete_authored=True)
        summary = _summary(meso, cell)
        assert summary["missed"] == 1
        assert summary["rpe"] == "9"
