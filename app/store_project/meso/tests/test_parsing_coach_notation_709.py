"""#709: coach notation and the new predicates.

Covers `225 for 5`, `1x5 @ 225`, `225x5 @8`, plus a pin that no string that
was a set before changes.
"""

import json

import pytest

from store_project.meso.parsing import parse_performed
from store_project.meso.parsing import performed_set_values
from store_project.meso.parsing import reads_as_one_set

_CORPUS_JSON = r"""{
"0": {
"kind": "set",
"load": "0",
"raw": "0"
},
"1,000": {
"kind": "set",
"load": "1000",
"raw": "1,000"
},
"1,000 x 5": {
"kind": "set",
"load": "1000",
"raw": "1,000 x 5",
"reps": 5
},
"1,000 x 5, RPE 8": {
"kind": "set",
"load": "1000",
"raw": "1,000 x 5, RPE 8",
"reps": 5,
"rpe": "8"
},
"1,000,000 x 5": {
"kind": "set",
"load": "1000000",
"raw": "1,000,000 x 5",
"reps": 5
},
"10 @ 5": {
"kind": "set",
"load": "5",
"raw": "10 @ 5",
"reps": 10
},
"100": {
"kind": "set",
"load": "100",
"raw": "100"
},
"100 x 100": {
"kind": "set",
"load": "100",
"raw": "100 x 100",
"reps": 100
},
"100 x 5": {
"kind": "set",
"load": "100",
"raw": "100 x 5",
"reps": 5
},
"100kg x 5": {
"kind": "set",
"load": "100kg",
"raw": "100kg x 5",
"reps": 5
},
"100kg x 5, RPE 8": {
"kind": "set",
"load": "100kg",
"raw": "100kg x 5, RPE 8",
"reps": 5,
"rpe": "8"
},
"100kgx5": {
"kind": "set",
"load": "100kg",
"raw": "100kgx5",
"reps": 5
},
"100kgx5, RPE 8": {
"kind": "set",
"load": "100kg",
"raw": "100kgx5, RPE 8",
"reps": 5,
"rpe": "8"
},
"100lb": {
"kind": "set",
"load": "100lb",
"raw": "100lb"
},
"102.5kg": {
"kind": "set",
"load": "102.5kg",
"raw": "102.5kg"
},
"102.5kg x 3": {
"kind": "set",
"load": "102.5kg",
"raw": "102.5kg x 3",
"reps": 3
},
"105": {
"kind": "set",
"load": "105",
"raw": "105"
},
"12 @ 9": {
"kind": "set",
"load": "9",
"raw": "12 @ 9",
"reps": 12
},
"12,345 x 2": {
"kind": "set",
"load": "12345",
"raw": "12,345 x 2",
"reps": 2
},
"120": {
"kind": "set",
"load": "120",
"raw": "120"
},
"120 x 5": {
"kind": "set",
"load": "120",
"raw": "120 x 5",
"reps": 5
},
"120 x 5, RPE 8": {
"kind": "set",
"load": "120",
"raw": "120 x 5, RPE 8",
"reps": 5,
"rpe": "8"
},
"120 x 5, RPE 9": {
"kind": "set",
"load": "120",
"raw": "120 x 5, RPE 9",
"reps": 5,
"rpe": "9"
},
"120.0 x 5": {
"kind": "set",
"load": "120.0",
"raw": "120.0 x 5",
"reps": 5
},
"135": {
"kind": "set",
"load": "135",
"raw": "135"
},
"135x5": {
"kind": "set",
"load": "135",
"raw": "135x5",
"reps": 5
},
"140": {
"kind": "set",
"load": "140",
"raw": "140"
},
"150": {
"kind": "set",
"load": "150",
"raw": "150"
},
"150 x 5": {
"kind": "set",
"load": "150",
"raw": "150 x 5",
"reps": 5
},
"175": {
"kind": "set",
"load": "175",
"raw": "175"
},
"185 x 8": {
"kind": "set",
"load": "185",
"raw": "185 x 8",
"reps": 8
},
"2": {
"kind": "set",
"load": "2",
"raw": "2"
},
"2,500 x 3": {
"kind": "set",
"load": "2500",
"raw": "2,500 x 3",
"reps": 3
},
"225": {
"kind": "set",
"load": "225",
"raw": "225"
},
"225 X 5": {
"kind": "set",
"load": "225",
"raw": "225 X 5",
"reps": 5
},
"225 X 5-8": {
"kind": "set",
"load": "225",
"raw": "225 X 5-8",
"reps_range": [
5,
8
]
},
"225 lb x 5": {
"kind": "set",
"load": "225lb",
"raw": "225 lb x 5",
"reps": 5
},
"225 lb x 5, RPE 8": {
"kind": "set",
"load": "225lb",
"raw": "225 lb x 5, RPE 8",
"reps": 5,
"rpe": "8"
},
"225 lbs x 5": {
"kind": "set",
"load": "225lbs",
"raw": "225 lbs x 5",
"reps": 5
},
"225 lbx5": {
"kind": "set",
"load": "225lb",
"raw": "225 lbx5",
"reps": 5
},
"225 lbx5, RPE 8": {
"kind": "set",
"load": "225lb",
"raw": "225 lbx5, RPE 8",
"reps": 5,
"rpe": "8"
},
"225 x 30 seconds": {
"duration": "30seconds",
"kind": "set",
"load": "225",
"raw": "225 x 30 seconds"
},
"225 x 30s": {
"duration": "30s",
"kind": "set",
"load": "225",
"raw": "225 x 30s"
},
"225 x 5": {
"kind": "set",
"load": "225",
"raw": "225 x 5",
"reps": 5
},
"225 x 5 breaths": {
"kind": "set",
"load": "225",
"raw": "225 x 5 breaths",
"reps": 5,
"unit": "breaths"
},
"225 x 5, RPE 8": {
"kind": "set",
"load": "225",
"raw": "225 x 5, RPE 8",
"reps": 5,
"rpe": "8"
},
"225 x 5, RPE 8.0": {
"kind": "set",
"load": "225",
"raw": "225 x 5, RPE 8.0",
"reps": 5,
"rpe": "8.0"
},
"225 x 5,300 x 3": {
"kind": "set",
"load": "225",
"raw": "225 x 5,300 x 3",
"reps": 5
},
"225 x 5-8": {
"kind": "set",
"load": "225",
"raw": "225 x 5-8",
"reps_range": [
5,
8
]
},
"225 x 8 each": {
"kind": "set",
"load": "225",
"raw": "225 x 8 each",
"reps": 8,
"unit": "each"
},
"225 x 8-10 each": {
"kind": "set",
"load": "225",
"raw": "225 x 8-10 each",
"reps_range": [
8,
10
],
"unit": "each"
},
"225 x AMRAP": {
"amrap": true,
"kind": "set",
"load": "225",
"raw": "225 x AMRAP"
},
"225lb": {
"kind": "set",
"load": "225lb",
"raw": "225lb"
},
"225lb x 5": {
"kind": "set",
"load": "225lb",
"raw": "225lb x 5",
"reps": 5
},
"225lb x 5, RPE 8": {
"kind": "set",
"load": "225lb",
"raw": "225lb x 5, RPE 8",
"reps": 5,
"rpe": "8"
},
"225lbx5": {
"kind": "set",
"load": "225lb",
"raw": "225lbx5",
"reps": 5
},
"225lbx5, RPE 8": {
"kind": "set",
"load": "225lb",
"raw": "225lbx5, RPE 8",
"reps": 5,
"rpe": "8"
},
"225x5": {
"kind": "set",
"load": "225",
"raw": "225x5",
"reps": 5
},
"225x5, 230x3": {
"kind": "set",
"load": "225",
"raw": "225x5, 230x3",
"reps": 5
},
"225x5, RPE 8": {
"kind": "set",
"load": "225",
"raw": "225x5, RPE 8",
"reps": 5,
"rpe": "8"
},
"225x5,100% effort": {
"kind": "set",
"load": "225",
"raw": "225x5,100% effort",
"reps": 5
},
"225x5,230x3": {
"kind": "set",
"load": "225",
"raw": "225x5,230x3",
"reps": 5
},
"230": {
"kind": "set",
"load": "230",
"raw": "230"
},
"230 x 3": {
"kind": "set",
"load": "230",
"raw": "230 x 3",
"reps": 3
},
"235": {
"kind": "set",
"load": "235",
"raw": "235"
},
"235 x 3": {
"kind": "set",
"load": "235",
"raw": "235 x 3",
"reps": 3
},
"240": {
"kind": "set",
"load": "240",
"raw": "240"
},
"240 x 2": {
"kind": "set",
"load": "240",
"raw": "240 x 2",
"reps": 2
},
"3": {
"kind": "set",
"load": "3",
"raw": "3"
},
"3 @ 10": {
"kind": "set",
"load": "10",
"raw": "3 @ 10",
"reps": 3
},
"3 x 10, 102.5 kg": {
"kind": "set",
"load": "3",
"raw": "3 x 10, 102.5 kg",
"reps": 10
},
"3 x 10, 30lbs": {
"kind": "set",
"load": "3",
"raw": "3 x 10, 30lbs",
"reps": 10
},
"3 x 12": {
"kind": "set",
"load": "3",
"raw": "3 x 12",
"reps": 12
},
"3 x 12-15": {
"kind": "set",
"load": "3",
"raw": "3 x 12-15",
"reps_range": [
12,
15
]
},
"3 x 15e": {
"kind": "set",
"load": "3",
"raw": "3 x 15e",
"reps": 15,
"unit": "each"
},
"3 x 1m": {
"duration": "1m",
"kind": "set",
"load": "3",
"raw": "3 x 1m"
},
"3 x 45s": {
"duration": "45s",
"kind": "set",
"load": "3",
"raw": "3 x 45s"
},
"3 x 5 breaths": {
"kind": "set",
"load": "3",
"raw": "3 x 5 breaths",
"reps": 5,
"unit": "breaths"
},
"3 x 8 each": {
"kind": "set",
"load": "3",
"raw": "3 x 8 each",
"reps": 8,
"unit": "each"
},
"3 x AMRAP": {
"amrap": true,
"kind": "set",
"load": "3",
"raw": "3 x AMRAP"
},
"3 \u00d7 12": {
"kind": "set",
"load": "3",
"raw": "3 \u00d7 12",
"reps": 12
},
"30lbs": {
"kind": "set",
"load": "30lbs",
"raw": "30lbs"
},
"30lbs X 8 each": {
"kind": "set",
"load": "30lbs",
"raw": "30lbs X 8 each",
"reps": 8,
"unit": "each"
},
"30lbs x 2 each": {
"kind": "set",
"load": "30lbs",
"raw": "30lbs x 2 each",
"reps": 2,
"unit": "each"
},
"30lbs x 8 each": {
"kind": "set",
"load": "30lbs",
"raw": "30lbs x 8 each",
"reps": 8,
"unit": "each"
},
"30s @ 225": {
"duration": "30s",
"kind": "set",
"load": "225",
"raw": "30s @ 225"
},
"315 x 5": {
"kind": "set",
"load": "315",
"raw": "315 x 5",
"reps": 5
},
"3x12": {
"kind": "set",
"load": "3",
"raw": "3x12",
"reps": 12
},
"3x5, 225": {
"kind": "set",
"load": "3",
"raw": "3x5, 225",
"reps": 5
},
"3x5, RPE 8, 80% 1RM": {
"kind": "set",
"load": "3",
"raw": "3x5, RPE 8, 80% 1RM",
"reps": 5,
"rpe": "8"
},
"4 x 12": {
"kind": "set",
"load": "4",
"raw": "4 x 12",
"reps": 12
},
"4 x 6 each": {
"kind": "set",
"load": "4",
"raw": "4 x 6 each",
"reps": 6,
"unit": "each"
},
"4 x 6, 85%": {
"kind": "set",
"load": "4",
"raw": "4 x 6, 85%",
"reps": 6
},
"4 x 6, RPE 9, 225": {
"kind": "set",
"load": "4",
"raw": "4 x 6, RPE 9, 225",
"reps": 6,
"rpe": "9"
},
"5": {
"kind": "set",
"load": "5",
"raw": "5"
},
"5 @ 1,000": {
"kind": "set",
"load": "1000",
"raw": "5 @ 1,000",
"reps": 5
},
"5 @ 1,000, RPE 8": {
"kind": "set",
"load": "1000",
"raw": "5 @ 1,000, RPE 8",
"reps": 5,
"rpe": "8"
},
"5 @ 100kg": {
"kind": "set",
"load": "100kg",
"raw": "5 @ 100kg",
"reps": 5
},
"5 @ 100kg, RPE 8": {
"kind": "set",
"load": "100kg",
"raw": "5 @ 100kg, RPE 8",
"reps": 5,
"rpe": "8"
},
"5 @ 225": {
"kind": "set",
"load": "225",
"raw": "5 @ 225",
"reps": 5
},
"5 @ 225 lb": {
"kind": "set",
"load": "225lb",
"raw": "5 @ 225 lb",
"reps": 5
},
"5 @ 225 lb, RPE 8": {
"kind": "set",
"load": "225lb",
"raw": "5 @ 225 lb, RPE 8",
"reps": 5,
"rpe": "8"
},
"5 @ 225, RPE 8": {
"kind": "set",
"load": "225",
"raw": "5 @ 225, RPE 8",
"reps": 5,
"rpe": "8"
},
"5 @ 225lb": {
"kind": "set",
"load": "225lb",
"raw": "5 @ 225lb",
"reps": 5
},
"5 @ 225lb, RPE 8": {
"kind": "set",
"load": "225lb",
"raw": "5 @ 225lb, RPE 8",
"reps": 5,
"rpe": "8"
},
"5 @ 7.5": {
"kind": "set",
"load": "7.5",
"raw": "5 @ 7.5",
"reps": 5
},
"5 @ 8": {
"kind": "set",
"load": "8",
"raw": "5 @ 8",
"reps": 5
},
"5 reps @ 100kg": {
"kind": "set",
"load": "100kg",
"raw": "5 reps @ 100kg",
"reps": 5,
"unit": "reps"
},
"5 reps @ 100kg, RPE 8": {
"kind": "set",
"load": "100kg",
"raw": "5 reps @ 100kg, RPE 8",
"reps": 5,
"rpe": "8",
"unit": "reps"
},
"5 reps @ 225": {
"kind": "set",
"load": "225",
"raw": "5 reps @ 225",
"reps": 5,
"unit": "reps"
},
"5 reps @ 225 lb": {
"kind": "set",
"load": "225lb",
"raw": "5 reps @ 225 lb",
"reps": 5,
"unit": "reps"
},
"5 reps @ 225 lb, RPE 8": {
"kind": "set",
"load": "225lb",
"raw": "5 reps @ 225 lb, RPE 8",
"reps": 5,
"rpe": "8",
"unit": "reps"
},
"5 reps @ 225, RPE 8": {
"kind": "set",
"load": "225",
"raw": "5 reps @ 225, RPE 8",
"reps": 5,
"rpe": "8",
"unit": "reps"
},
"5 reps @ 225lb": {
"kind": "set",
"load": "225lb",
"raw": "5 reps @ 225lb",
"reps": 5,
"unit": "reps"
},
"5 reps @ 225lb, RPE 8": {
"kind": "set",
"load": "225lb",
"raw": "5 reps @ 225lb, RPE 8",
"reps": 5,
"rpe": "8",
"unit": "reps"
},
"5-8 @ 225": {
"kind": "set",
"load": "225",
"raw": "5-8 @ 225",
"reps_range": [
5,
8
]
},
"6": {
"kind": "set",
"load": "6",
"raw": "6"
},
"7": {
"kind": "set",
"load": "7",
"raw": "7"
},
"70": {
"kind": "set",
"load": "70",
"raw": "70"
},
"8": {
"kind": "set",
"load": "8",
"raw": "8"
},
"8 @ 2.5": {
"kind": "set",
"load": "2.5",
"raw": "8 @ 2.5",
"reps": 8
},
"8 each @ 225": {
"kind": "set",
"load": "225",
"raw": "8 each @ 225",
"reps": 8,
"unit": "each"
},
"8.0": {
"kind": "set",
"load": "8.0",
"raw": "8.0"
},
"80": {
"kind": "set",
"load": "80",
"raw": "80"
},
"80 x 8": {
"kind": "set",
"load": "80",
"raw": "80 x 8",
"reps": 8
},
"80%": {
"kind": "set",
"load": "80%",
"raw": "80%"
},
"82.5%": {
"kind": "set",
"load": "82.5%",
"raw": "82.5%"
},
"85%": {
"kind": "set",
"load": "85%",
"raw": "85%"
},
"85% x 5": {
"kind": "set",
"load": "85%",
"raw": "85% x 5",
"reps": 5
},
"9": {
"kind": "set",
"load": "9",
"raw": "9"
},
"90 x 5": {
"kind": "set",
"load": "90",
"raw": "90 x 5",
"reps": 5
},
"AMRAP @ 225": {
"amrap": true,
"kind": "set",
"load": "225",
"raw": "AMRAP @ 225"
},
"BW": {
"kind": "set",
"load": "BW",
"raw": "BW"
},
"BW x 12": {
"kind": "set",
"load": "BW",
"raw": "BW x 12",
"reps": 12
},
"bw": {
"kind": "set",
"load": "bw",
"raw": "bw"
},
"bw x 12": {
"kind": "set",
"load": "bw",
"raw": "bw x 12",
"reps": 12
},
"bw x 50": {
"kind": "set",
"load": "bw",
"raw": "bw x 50",
"reps": 50
}
}"""

# Every string drawn from the existing parser tests (plus the notation table
# below) that parsed to ``kind == "set"`` on main BEFORE the #709 extension,
# with its exact output. The extension may only add sets, never change one.
CORPUS = json.loads(_CORPUS_JSON)


@pytest.mark.parametrize("text", sorted(CORPUS))
def test_existing_sets_parse_identically(text):
    assert json.loads(json.dumps(parse_performed(text))) == CORPUS[text]


def _forms():
    loads = [
        ("225", "225"),
        ("225lb", "225lb"),
        ("225 lb", "225lb"),
        ("100kg", "100kg"),
    ]
    bases = ["{L}x5", "{L} x 5", "5 @ {L}", "1x5 @ {L}", "5 reps @ {L}", "{L} for 5"]
    for base in bases:
        for load, norm in loads:
            for suffix, rpe in SUFFIXES:
                yield base.format(L=load) + suffix, 5, norm, rpe
    for base in ["BW x 10", "10 @ BW", "BW for 10", "10 reps @ BW", "1x10 @ BW"]:
        for suffix, rpe in SUFFIXES:
            yield base + suffix, 10, "BW", rpe


SUFFIXES = [("", None), (" @8", "8"), (" @ 8", "8"), (" RPE 8", "8"), (", RPE 8", "8")]


@pytest.mark.parametrize(("text", "reps", "load", "rpe"), list(_forms()))
def test_notation_table(text, reps, load, rpe):
    parsed = parse_performed(text)
    assert parsed["kind"] == "set", text
    assert parsed["reps"] == reps
    assert parsed["load"].lower() == load.lower()
    assert parsed.get("rpe") == rpe
    assert reads_as_one_set(text) is True
    assert performed_set_values(text)


@pytest.mark.parametrize(
    "text", ["3x5 @ 225", "3 x 5 @ 225", "225 @8", "225 x 5 @ 95", "225x5 @11"]
)
def test_not_sets(text):
    assert parse_performed(text)["kind"] != "set"
    assert reads_as_one_set(text) is False


def test_five_at_eight_unchanged():
    assert parse_performed("5 @ 8") == {
        "reps": 5,
        "load": "8",
        "kind": "set",
        "raw": "5 @ 8",
    }
    assert reads_as_one_set("5 @ 8") is False


def test_three_by_five_is_a_set_but_not_one_set():
    parsed = parse_performed("3x5")
    assert parsed["kind"] == "set"
    assert parsed["load"] == "3"
    assert reads_as_one_set("3x5") is False


@pytest.mark.parametrize(
    "text",
    [
        "3 x 8-10",
        "10x10",
        "not a set",
        "",
        # Review round 1: a percentage is how a coach prescribes a load, and a
        # bare decimal of 10 or less reads as an RPE.
        "5 @ 70%",
        "85% x 5",
        "5 @ 70% 1RM",
        "5 @ 8.5",
        "4 @ 9.5",
        # Review round 2: ranges and AMRAP are targets, and a bare number up
        # to 20 before an ``x`` is a set count (speed work, EMOMs).
        "225 x 8-10",
        "8-10 @ 135",
        "AMRAP @ 135",
        "12x2",
        "20 x 4 @ 8",
        "15 x 10",
    ],
)
def test_strict_default_false(text):
    assert reads_as_one_set(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "25x5",
        "12kg x 2",
        "5 @ 20",
        "5 @ 8kg",
        "225x5 @8",
        "1x5 @ 225",
        "225 for 5",
        # #720: one set of 5 at 225, no longer 1 lb × 5.
        "1x5, 225",
    ],
)
def test_strict_default_true(text):
    assert reads_as_one_set(text) is True


def test_performed_set_values_matches_writer_test():
    assert performed_set_values("225x5")
    assert performed_set_values("225")
    assert not performed_set_values("skip")
    assert not performed_set_values("felt great")
    assert not performed_set_values("")
