"""``parse_prescription`` over the real template corpus (Phase 2a).

Every case here is a VERBATIM cell from Lance's template library
(docs/meso/spreadsheet-parity-plan.md §1, docs/meso/fixtures/templates/) or
from the filled client workbook (§2.6) — not invented notation. The parser is
tolerant/best-effort: asserting a subset of keys is the point; a case that
parses to nothing still carries ``raw``.

``parse_performed`` (5a, docs/meso/parse-at-commit-plan.md §3) gets its own
pinned corpus below, one function per §3 table row plus the load-first
inversion assertion that is the whole point of that slice.
"""

import pytest

from store_project.meso.parsing import parse_performed
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


# ---------------------------------------------------------------------------
# parse_performed (5a) — docs/meso/parse-at-commit-plan.md §3
# ---------------------------------------------------------------------------


def test_performed_empty_and_none_parse_to_none():
    assert parse_performed(None) is None
    assert parse_performed("") is None
    assert parse_performed("   ") is None


def test_performed_load_first_inversion():
    # THE point of 5a: prescription grammar reads a leading "N x M" as sets;
    # performed grammar reads it as load x reps.
    prescribed = parse_prescription("225 x 5")
    assert prescribed["sets"] == 225
    assert prescribed["reps"] == 5

    performed = parse_performed("225 x 5")
    assert performed["kind"] == "set"
    assert performed["load"] == "225"
    assert performed["reps"] == 5


def test_performed_plain_set_no_space():
    parsed = parse_performed("135x5")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "135"
    assert parsed["reps"] == 5


def test_performed_set_with_rpe():
    parsed = parse_performed("225 x 5, RPE 8")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "225"
    assert parsed["reps"] == 5
    assert parsed["rpe"] == "8"


def test_performed_suffixed_and_percent_loads():
    parsed = parse_performed("30lbs x 8 each")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "30lbs"
    assert parsed["reps"] == 8
    assert parsed["unit"] == "each"

    parsed = parse_performed("102.5kg x 3")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "102.5kg"
    assert parsed["reps"] == 3

    parsed = parse_performed("85% x 5")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "85%"
    assert parsed["reps"] == 5

    parsed = parse_performed("bw x 12")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "bw"
    assert parsed["reps"] == 12


def test_performed_at_form():
    parsed = parse_performed("5 @ 225")
    assert parsed["kind"] == "set"
    assert parsed["reps"] == 5
    assert parsed["load"] == "225"


def test_performed_bare_load_is_a_partial_set():
    parsed = parse_performed("225")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "225"
    assert "reps" not in parsed


@pytest.mark.parametrize("text", ["skip", "-", "—"])
def test_performed_skip_forms(text):
    parsed = parse_performed(text)
    assert parsed["kind"] == "skip"


@pytest.mark.parametrize("text", ["DB pullover", "R SL L glute max"])
def test_performed_swap_forms(text):
    parsed = parse_performed(text)
    assert parsed["kind"] == "swap"


@pytest.mark.parametrize("text", ["felt tight", "paired with lat hang"])
def test_performed_note_forms(text):
    parsed = parse_performed(text)
    assert parsed["kind"] == "note"


@pytest.mark.parametrize("text", ["225 x", "2255x5", "225 x five", "225x5x5"])
def test_performed_unresolved_set_forms(text):
    parsed = parse_performed(text)
    assert parsed["kind"] == "unresolved-set"


@pytest.mark.parametrize("text", ["225 x five", "225x5x5", "225 x ?!"])
def test_an_x_with_unreadable_reps_warns_rather_than_logging_load_only(text):
    """An ``x`` means a set was attempted — a load-only salvage would lie.

    The left side parses as a plausible load, so it is tempting to return
    ``{"load": "225"}`` and call it a partial set. That would persist a repless
    ``LoggedSet`` that can never count toward a record, while suppressing the
    warning — the athlete sees no complaint and reasonably believes it logged.
    Bare ``225`` (no ``x``, no attempt at reps) stays a legitimate partial.
    """
    parsed = parse_performed(text)
    assert parsed["kind"] == "unresolved-set"
    assert parsed.get("warn") is True
    assert "load" not in parsed


def test_a_bare_load_is_still_a_partial_set():
    parsed = parse_performed("225")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "225"
    assert "reps" not in parsed


def test_a_loaded_timed_set_survives_the_reps_guard():
    # ``duration`` counts as recognized right-hand data — this is a real
    # timed set (225 held for 30s), not a fat-finger.
    parsed = parse_performed("225 x 30s")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "225"
    assert parsed["duration"] == "30s"


@pytest.mark.parametrize("text", ["30s", "20-60m"])
def test_performed_duration_forms(text):
    parsed = parse_performed(text)
    assert parsed["kind"] == "duration"


def test_performed_warns_only_on_unresolved_set():
    assert parse_performed("225 x").get("warn") is True
    assert parse_performed("2255x5").get("warn") is True

    assert not parse_performed("skip").get("warn")
    assert not parse_performed("DB pullover").get("warn")
    assert not parse_performed("felt tight").get("warn")
    assert not parse_performed("225 x 5").get("warn")


def test_performed_one_set_per_line_only():
    # Multi-set-per-line is explicitly out of scope (plan §3) — only the
    # first recognized set on the line is returned.
    parsed = parse_performed("225x5, 230x3")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "225"
    assert parsed["reps"] == 5


def test_performed_multiline_cell_classifies_first_line_only():
    parsed = parse_performed("225 x 5\npaired with lat hang")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "225"
    assert parsed["reps"] == 5
    assert parsed["raw"].endswith("lat hang")


@pytest.mark.parametrize(
    "text",
    [
        "225 x 5",
        "135x5",
        "225 x 5, RPE 8",
        "30lbs x 8 each",
        "102.5kg x 3",
        "85% x 5",
        "bw x 12",
        "5 @ 225",
        "225",
        "skip",
        "-",
        "—",
        "DB pullover",
        "R SL L glute max",
        "felt tight",
        "paired with lat hang",
        "225 x",
        "2255x5",
        "30s",
        "20-60m",
    ],
)
def test_performed_corpus_never_raises_and_always_carries_raw(text):
    parsed = parse_performed(text)
    assert parsed is not None
    assert parsed["raw"] == text.strip()
    assert parsed["kind"] in {
        "set",
        "skip",
        "swap",
        "note",
        "unresolved-set",
        "duration",
    }


@pytest.mark.parametrize(
    "text", ["Box squat 225", "max effort 3", "Flexion 3", "Hex bar 185"]
)
def test_a_plain_x_in_a_name_is_not_a_set_attempt(text):
    """``x`` has to be an OPERATOR before it can mean a fat-fingered set.

    Searching for a bare ``x`` anywhere fires on ordinary exercise names that
    happen to contain one — "Box squat", "Flexion", "Hex bar" — so an athlete
    typing a perfectly good swap with a load next to it got warned at.
    """
    parsed = parse_performed(text)
    assert parsed["kind"] != "unresolved-set"
    assert not parsed.get("warn")


@pytest.mark.parametrize("text", ["225 x", "x 5", "5 @", "225 x five"])
def test_a_digit_adjacent_operator_still_warns(text):
    parsed = parse_performed(text)
    assert parsed["kind"] == "unresolved-set"
    assert parsed["warn"] is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("225 x 5", "5"),
        ("225 x 5-8", "5-8"),
        ("225 x 30s", "30s"),
        ("225 x AMRAP", "AMRAP"),
        ("225", ""),
    ],
)
def test_performed_reps_text_keeps_every_recognized_form(text, expected):
    """A set's right-hand side lands in one of four keys, not just ``reps``.

    Reading only ``reps`` blanked ranges, timed sets and AMRAP, so a real
    performance rendered as ``— @ 225`` in coach results.
    """
    from store_project.meso.parsing import performed_reps_text

    assert performed_reps_text(parse_performed(text)) == expected


def test_performed_reps_text_tolerates_none():
    from store_project.meso.parsing import performed_reps_text

    assert performed_reps_text(None) == ""


@pytest.mark.parametrize("text", ["225 X 5", "30lbs X 8 each", "225 X 5-8"])
def test_an_uppercase_x_is_still_a_set_operator(text):
    """Phone keyboards auto-capitalize, so ``225 X 5`` is everyday input.

    The split was case-sensitive while `_looks_like_set_attempt` was not, so an
    uppercase X produced the worst combination: a real set refused, tinted as a
    fat-finger, and never logged.
    """
    parsed = parse_performed(text)
    assert parsed["kind"] == "set"
    assert not parsed.get("warn")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("225 x 8 each", "8 each"),
        ("225 x 5 breaths", "5 breaths"),
        ("225 x 8-10 each", "8-10 each"),
        ("225 x 5", "5"),
    ],
)
def test_performed_reps_text_keeps_the_unit_suffix(text, expected):
    """``8 each`` means something different from a bare ``8``."""
    from store_project.meso.parsing import performed_reps_text

    assert performed_reps_text(parse_performed(text)) == expected
