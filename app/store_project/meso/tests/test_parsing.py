"""``parse_prescription`` over the real template corpus (Phase 2a).

Every case here is a VERBATIM cell from Lance's template library
(docs/meso/spreadsheet-parity-plan.md §1, docs/meso/fixtures/templates/) or
from the filled client workbook (§2.6) — not invented notation. The parser is
tolerant/best-effort: asserting a subset of keys is the point; a case that
parses to nothing still carries ``raw``.
"""

import pytest

from store_project.meso.parsing import parse_prescription


def test_empty_and_none_parse_to_none():
    assert parse_prescription(None) is None
    assert parse_prescription("") is None
    assert parse_prescription("   ") is None


def test_plain_sets_by_reps():
    parsed = parse_prescription("3 x 12")
    assert parsed["sets"] == 3
    assert parsed["reps"] == 12
    assert parsed["raw"] == "3 x 12"


def test_no_space_and_multiplication_sign():
    assert parse_prescription("3x12")["reps"] == 12
    assert parse_prescription("3 × 12")["reps"] == 12


def test_rep_range():
    parsed = parse_prescription("3 x 12-15")
    assert parsed["sets"] == 3
    assert parsed["reps_range"] == (12, 15)
    assert "reps" not in parsed


def test_each_suffix():
    parsed = parse_prescription("3 x 8 each")
    assert parsed["reps"] == 8
    assert parsed["unit"] == "each"


def test_abbreviated_each():
    parsed = parse_prescription("3 x 15e")
    assert parsed["reps"] == 15
    assert parsed["unit"] == "each"


def test_timed_sets_are_durations_not_reps():
    parsed = parse_prescription("3 x 45s")
    assert parsed["sets"] == 3
    assert parsed["duration"] == "45s"
    assert "reps" not in parsed

    parsed = parse_prescription("3 x 1m")
    assert parsed["duration"] == "1m"


def test_breath_unit():
    parsed = parse_prescription("3 x 5 breaths")
    assert parsed["sets"] == 3
    assert parsed["reps"] == 5
    assert parsed["unit"] == "breaths"


def test_bare_duration_cells():
    assert parse_prescription("20-60m")["duration"] == "20-60m"
    assert parse_prescription("20-75 min")["duration"] == "20-75min"
    assert parse_prescription("15'")["duration"] == "15'"


def test_up_to_hedge():
    parsed = parse_prescription("Up to 8 x 8s")
    assert parsed["sets"] == 8
    assert parsed["duration"] == "8s"


def test_bare_once():
    parsed = parse_prescription("1x")
    assert parsed["sets"] == 1
    assert "reps" not in parsed


def test_placeholder_reps():
    parsed = parse_prescription("3 x ?")
    assert parsed["sets"] == 3
    assert "reps" not in parsed


def test_amrap():
    assert parse_prescription("AMRAP")["amrap"] is True
    assert parse_prescription("3 x AMRAP")["sets"] == 3
    assert parse_prescription("3 x AMRAP")["amrap"] is True


def test_full_prescription_with_rpe_and_load():
    parsed = parse_prescription("4 x 6, RPE 9, 225")
    assert parsed["sets"] == 4
    assert parsed["reps"] == 6
    assert parsed["rpe"] == "9"
    assert parsed["load"] == "225"


def test_at_separator():
    parsed = parse_prescription("4 x 6 @ RPE 9")
    assert parsed["rpe"] == "9"


def test_rpe_only_subrow_cells():
    assert parse_prescription("RPE 8")["rpe"] == "8"
    assert parse_prescription("RPE 6-7")["rpe"] == "6-7"


def test_percent_load():
    parsed = parse_prescription("4 x 6, 85%")
    assert parsed["load"] == "85%"


def test_suffixed_load():
    assert parse_prescription("3 x 10, 30lbs")["load"] == "30lbs"
    assert parse_prescription("3 x 10, 102.5 kg")["load"] == "102.5kg"


def test_logged_execution_load_first():
    # A filled client cell (§2.6): what the athlete actually did.
    parsed = parse_prescription("30lbs x 2 each")
    assert parsed["load"] == "30lbs"
    assert parsed["reps"] == 2
    assert parsed["unit"] == "each"


def test_skip_cell():
    parsed = parse_prescription("skip")
    assert parsed["skip"] is True


def test_circuit_cell_survives_as_raw():
    # A whole circuit packed into one cell parses to at least ``raw`` —
    # never raises, never blocks entry.
    text = "A) EDT 1. RDL x 6 2. Bench press x 6"
    parsed = parse_prescription(text)
    assert parsed["raw"] == text


def test_note_line_survives_as_raw():
    text = "paired with lat hang or plank to downward dog"
    parsed = parse_prescription(text)
    assert parsed["raw"] == text


def test_multiline_cell_classifies_first_line_only():
    parsed = parse_prescription("3 x 12\npaired with lat hang")
    assert parsed["sets"] == 3
    assert parsed["reps"] == 12
    assert parsed["raw"].endswith("lat hang")


@pytest.mark.parametrize(
    "text",
    [
        "3 x 12",
        "4 x 12",
        "3 x 8 each",
        "4 x 6 each",
        "3 x 12-15",
        "3 x 15e",
        "3 x 45s",
        "3 x 1m",
        "3 x 5 breaths",
        "20-60m",
        "15'",
        "Up to 8 x 8s",
        "1x",
        "3 x ?",
        "AMRAP",
        "A) EDT 1. RDL x 6 2. Bench press x 6",
        "skip",
        "DB pullover",
        "RPE 8",
        "4 x 6, RPE 9, 225",
    ],
)
def test_corpus_never_raises_and_always_carries_raw(text):
    parsed = parse_prescription(text)
    assert parsed is not None
    assert parsed["raw"] == text.strip()
