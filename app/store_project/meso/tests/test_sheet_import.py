"""Phase 3 (spreadsheet parity §5) — the template importer.

``sheet_import.parse_workbook`` turns a Drive-exported template ``.xlsx``
into a ``build_block``-ready spec; ``meso_import_template`` wraps it in
template-``Plan`` + ``Mesocycle`` bookkeeping. Tests run against the five
real (anonymized) fixtures under ``docs/meso/fixtures/templates/``:

- tab selection (102's hidden legacy tab skipped; 101's metadata tabs passed
  over for 'Program 101');
- per-fixture day/exercise/week counts and spot-checked known cells —
  verbatim text, tempo float-coercion, merged name/rest/note capture, the
  102 RPE sub-row folded as per-week sub-lines, 601's separator + packed
  circuit/EDT rows;
- the command: single-file import builds the tree, a 3-file family imports
  as ONE plan with 3 ordered blocks, re-running updates in place (no
  duplicates), and an unknown ``--owner`` errors cleanly.
"""

from pathlib import Path

import pytest
from django.core.management import CommandError
from django.core.management import call_command

from store_project.meso.models import ExerciseSlot
from store_project.meso.models import Mesocycle
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.meso.models import Session
from store_project.meso.models import SessionSlot
from store_project.meso.models import Week
from store_project.meso.sheet_import import SheetImportError
from store_project.meso.sheet_import import coerce_text
from store_project.meso.sheet_import import parse_workbook
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

FIXTURES = (
    Path(__file__).resolve().parents[4] / "docs" / "meso" / "fixtures" / "templates"
)


def parse(name):
    return parse_workbook(FIXTURES / f"{name}.xlsx")


def day_rows(block, day_number):
    """(exercises, cells-per-week-1) for one day of a parsed block."""
    spec = block.block_spec
    day = next(d for d in spec["days"] if d["day_number"] == day_number)
    week1 = next(w for w in spec["weeks"] if w["index"] == 1)
    return day["exercises"], week1["cells"][day_number]


class TestTabSelection:
    def test_101_picks_the_program_tab_over_metadata_tabs(self):
        # 101 bundles Athlete / Warm Up / FAQ / Periodization tabs — the Warm
        # Up tab even has an 'Exercise' header, but no 'Week N' columns.
        assert parse("101").tab == "Program 101"

    def test_102_skips_the_hidden_legacy_tab(self):
        # 102's first tab is the hidden legacy structured grid ('Program').
        assert parse("102").tab == "102"

    def test_single_tab_workbooks(self):
        assert parse("402").tab == "402"
        assert parse("601").tab == "601"
        assert parse("103").tab == "103"

    def test_workbook_without_a_grid_raises(self, tmp_path):
        from openpyxl import Workbook

        wb = Workbook()
        wb.active["A1"] = "nothing here"
        path = tmp_path / "empty.xlsx"
        wb.save(path)
        with pytest.raises(SheetImportError):
            parse_workbook(path)


class TestFixtureCounts:
    @pytest.mark.parametrize(
        ("name", "days", "exercises", "weeks", "cells"),
        [
            ("101", 7, 22, 4, 96),
            ("102", 7, 22, 4, 100),
            ("103", 7, 22, 4, 100),
            ("402", 7, 19, 4, 72),
            ("601", 7, 13, 4, 40),
        ],
    )
    def test_days_exercises_weeks_cells(self, name, days, exercises, weeks, cells):
        block = parse(name)
        assert block.day_count == days
        assert block.exercise_count == exercises
        assert block.week_count == weeks
        assert block.cell_count == cells

    def test_every_fixture_reports_skipped_chrome(self):
        for name in ("101", "102", "103", "402", "601"):
            reasons = {s.reason for s in parse(name).skipped}
            assert "date row" in reasons, name
            assert "banner / pre-grid row" in reasons, name


class Test402KnownCells:
    """402's first day (header r3, first exercise row r4)."""

    def test_first_rows_verbatim(self):
        exercises, cells = day_rows(parse("402"), 1)
        assert exercises[0]["name"] == "A) Squat jump"
        assert exercises[0]["tempo"] == "EXP"
        assert exercises[0]["rest"] == "75s"
        assert exercises[0]["note"] == "Max speed!"
        assert cells[0]["text"] == "3 x 3"

    def test_tempo_float_coerces_to_int_spelling(self):
        # Sheets stores the numeric tempos as floats — openpyxl reads 201.0.
        exercises, _ = day_rows(parse("402"), 1)
        assert exercises[1]["name"] == "B) Split squat"
        assert exercises[1]["tempo"] == "201"

    def test_merged_rest_and_note_captured_from_block_top(self):
        exercises, cells = day_rows(parse("402"), 1)
        assert exercises[1]["rest"] == "2m"
        assert exercises[1]["note"] == "Max fatigue"
        assert cells[1]["text"] == "3 x 10"

    def test_placeholder_and_each_cells_stay_verbatim(self):
        _, cells = day_rows(parse("402"), 1)
        assert cells[3]["text"] == "3 x ?"
        assert cells[4]["text"] == "3 x 6 each"

    def test_no_rpe_subrow_means_no_lines(self):
        block = parse("402")
        for week in block.block_spec["weeks"]:
            for cells in week["cells"].values():
                assert all(cell["lines"] == [] for cell in cells)

    def test_cell_less_rest_day_row(self):
        exercises, cells = day_rows(parse("402"), 7)
        assert exercises == [{"name": "Off or walk"}]
        assert cells[0]["text"] == ""


class Test102RpeSubRow:
    def test_rpe_folds_into_each_weeks_cell_as_a_sub_line(self):
        # 102 Day 1 r4/r5: 'A) Front squat' with 'RPE 8|RPE 9|RPE 6-7|RPE 10'.
        block = parse("102")
        spec = block.block_spec
        by_index = {w["index"]: w["cells"][1][0] for w in spec["weeks"]}
        assert by_index[1] == {"text": "3 x 12", "lines": ["RPE 8"]}
        assert by_index[2] == {"text": "4 x 12", "lines": ["RPE 9"]}
        assert by_index[3] == {"text": "3 x 8", "lines": ["RPE 6-7"]}
        assert by_index[4] == {"text": "4 x 10", "lines": ["RPE 10"]}

    def test_rows_without_rpe_get_no_lines(self):
        _, cells = day_rows(parse("102"), 1)
        assert cells[1]["lines"] == []  # B) Front foot-elevated split squat


class Test601EdgeCases:
    def test_separator_imports_as_a_cell_less_freeform_row(self):
        exercises, cells = day_rows(parse("601"), 1)
        assert [e["name"].split("\n")[0] for e in exercises] == [
            "A) Escalating Density Training",
            "Rest 5 minutes",
            "B) Escalating Density Training",
            "C) Quadruped 1-arm plank",
        ]
        separator = exercises[1]
        assert separator == {"name": "Rest 5 minutes"}  # no tempo/rest/note
        assert cells[1] == {"text": "", "lines": []}

    def test_edt_packed_cell_is_one_row_with_verbatim_name(self):
        exercises, cells = day_rows(parse("601"), 1)
        assert "1. RDL x 6" in exercises[0]["name"]
        assert "2. Bench press x 6" in exercises[0]["name"]
        assert cells[0]["text"] == "15'"  # the time-cap prescription

    def test_circuit_day_is_one_packed_row(self):
        exercises, cells = day_rows(parse("601"), 6)
        assert len(exercises) == 1
        assert exercises[0]["name"].startswith("Bodybuilding circuit")
        assert "A1)" in exercises[0]["name"]
        assert cells[0]["text"] == "2x each"


class TestCoerceText:
    def test_integral_float_loses_the_artifact(self):
        assert coerce_text(201.0) == "201"

    def test_non_integral_float_and_strings_pass_through(self):
        assert coerce_text(602.5) == "602.5"
        assert coerce_text("3 x 12") == "3 x 12"
        assert coerce_text(None) == ""


class TestImportCommand:
    def test_single_file_import_creates_the_tree(self):
        owner = UserFactory()
        call_command(
            "meso_import_template", str(FIXTURES / "402.xlsx"), owner=owner.email
        )
        plan = Plan.objects.get(is_template=True, owner=owner)
        assert plan.title == "402"
        assert plan.relationship is None
        block = Mesocycle.objects.get(plan=plan)
        assert (block.name, block.order, block.week_count) == ("402", 0, 4)
        assert SessionSlot.objects.filter(mesocycle=block).count() == 7
        slots = ExerciseSlot.objects.filter(session_slot__mesocycle=block)
        assert slots.count() == 19
        assert Week.objects.filter(mesocycle=block).count() == 4
        assert Session.objects.filter(week__mesocycle=block).count() == 28
        # Dense line-0 grid: every slot × week has a cell.
        line0 = Prescription.objects.filter(
            exercise_slot__session_slot__mesocycle=block, line=0
        )
        assert line0.count() == 19 * 4

        split_squat = slots.get(name="B) Split squat")
        assert (split_squat.tempo, split_squat.rest) == ("201", "2m")
        assert split_squat.note == "Max fatigue"
        cell = Prescription.objects.get(
            exercise_slot=split_squat, week__index=1, line=0
        )
        assert cell.text == "3 x 10"

    def test_rpe_sub_lines_materialize_as_line_1(self):
        owner = UserFactory()
        call_command(
            "meso_import_template", str(FIXTURES / "102.xlsx"), owner=owner.email
        )
        front_squat = ExerciseSlot.objects.get(
            session_slot__mesocycle__plan__owner=owner, name="A) Front squat"
        )
        rpe = Prescription.objects.get(exercise_slot=front_squat, week__index=3, line=1)
        assert rpe.text == "RPE 6-7"

    def test_family_imports_as_one_plan_with_ordered_blocks(self):
        owner = UserFactory()
        call_command(
            "meso_import_template",
            str(FIXTURES / "101.xlsx"),
            str(FIXTURES / "102.xlsx"),
            str(FIXTURES / "103.xlsx"),
            owner=owner.email,
            title="Base 1-3",
        )
        plan = Plan.objects.get(is_template=True, owner=owner)
        assert plan.title == "Base 1-3"
        blocks = list(Mesocycle.objects.filter(plan=plan).order_by("order"))
        assert [(b.order, b.name) for b in blocks] == [
            (0, "Program 101"),
            (1, "102"),
            (2, "103"),
        ]

    def test_rerun_updates_in_place(self):
        owner = UserFactory()
        for _ in range(2):
            call_command(
                "meso_import_template", str(FIXTURES / "402.xlsx"), owner=owner.email
            )
        assert Plan.objects.filter(is_template=True, owner=owner).count() == 1
        plan = Plan.objects.get(is_template=True, owner=owner)
        assert Mesocycle.objects.filter(plan=plan).count() == 1
        assert (
            ExerciseSlot.objects.filter(session_slot__mesocycle__plan=plan).count()
            == 19
        )
        assert (
            Prescription.objects.filter(
                exercise_slot__session_slot__mesocycle__plan=plan
            ).count()
            == 19 * 4
        )

    def test_unknown_owner_errors_cleanly(self):
        with pytest.raises(CommandError, match="No user with email"):
            call_command(
                "meso_import_template",
                str(FIXTURES / "402.xlsx"),
                owner="nobody@example.com",
            )
        assert Plan.objects.count() == 0
