"""#720: ``1x5, 225`` is one set of 5 at 225, not 1 lb x 5 with 225 dropped.

Only a head of exactly ``1 x N`` moves; every other ``N x M, load`` shape keeps
its load-first parse (pinned by the #709 corpus), and the guards below pin the
neighbouring shapes that must not change.
"""

import json

import pytest

from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
from store_project.meso.parsing import parse_performed
from store_project.meso.parsing import reads_as_one_set
from store_project.meso.tests.test_parse_at_commit import coach_write
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import write_cell


def _norm(value):
    return json.loads(json.dumps(value))


def _set(raw, reps, load, **extra):
    return {"kind": "set", "raw": raw, "reps": reps, "load": load, **extra}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1x5, 225", _set("1x5, 225", 5, "225")),
        ("1 x 5, 225", _set("1 x 5, 225", 5, "225")),
        ("1x5, 225, RPE 8", _set("1x5, 225, RPE 8", 5, "225", rpe="8")),
        ("1 x 5, RPE 8, 225", _set("1 x 5, RPE 8, 225", 5, "225", rpe="8")),
        ("1x5,225", _set("1x5,225", 5, "225")),
        ("1 × 5, 225", _set("1 × 5, 225", 5, "225")),
        ("1X5, 225lb", _set("1X5, 225lb", 5, "225lb")),
        ("1x5, 225 lb", _set("1x5, 225 lb", 5, "225lb")),
        ("1x5, 100 kg", _set("1x5, 100 kg", 5, "100kg")),
        ("1x3, 102.5kg", _set("1x3, 102.5kg", 3, "102.5kg")),
        ("1x10, BW", _set("1x10, BW", 10, "BW")),
        ("1x1, 315", _set("1x1, 315", 1, "315")),
        # Review: empty segments and a trailing period are skipped, as the
        # RPE read already skips them.
        ("1x5,,225", _set("1x5,,225", 5, "225")),
        ("1x5, , 225", _set("1x5, , 225", 5, "225")),
        ("1x5, ., 225", _set("1x5, ., 225", 5, "225")),
        ("1x5, 225.", _set("1x5, 225.", 5, "225")),
    ],
)
def test_one_set_comma_load(text, expected):
    assert _norm(parse_performed(text)) == _norm(expected)


@pytest.mark.parametrize(
    "text",
    [
        "1x5, 2255",
        # Review: a comma inside the load splits it; storing ``22`` (in the
        # plan's unit) would be a different set from 22.5 kg.
        "1x5, 22,5kg",
        "1x1, 1,000",
    ],
)
def test_unreadable_load_is_unresolved_not_one_pound(text):
    assert parse_performed(text) == {
        "kind": "unresolved-set",
        "raw": text,
        "warn": True,
    }


@pytest.mark.parametrize("text", ["1x5, 225", "1 x 5, RPE 8, 225", "1x5, 100kg"])
def test_strict_default_true(text):
    assert reads_as_one_set(text) is True


@pytest.mark.parametrize("text", ["1x5, 8", "1x5, 85%", "3x5, 225"])
def test_strict_default_still_false(text):
    assert reads_as_one_set(text) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1x5", {"load": "1", "reps": 5, "kind": "set", "raw": "1x5"}),
        (
            "1 x 5 @ 225",
            {"reps": 5, "load": "225", "kind": "set", "raw": "1 x 5 @ 225"},
        ),
        ("225, 1x5", {"load": "225", "kind": "set", "raw": "225, 1x5"}),
        (
            "1 x 5-8, 225",
            {
                "load": "1",
                "reps_range": [5, 8],
                "kind": "set",
                "raw": "1 x 5-8, 225",
            },
        ),
        (
            "1x5, felt good",
            {"load": "1", "reps": 5, "kind": "set", "raw": "1x5, felt good"},
        ),
        (
            "1x225, 5",
            {"kind": "unresolved-set", "raw": "1x225, 5", "warn": True},
        ),
    ],
)
def test_unchanged_shapes(text, expected):
    assert _norm(parse_performed(text)) == expected


@pytest.mark.django_db
def test_athlete_line_logs_225_not_one_pound(client):
    s = seed()
    client.force_login(s.athlete)
    assert (
        write_cell(client, s.session, s.squat, 1, "1x5, 225, RPE 8").status_code == 200
    )
    row = LoggedSet.objects.get(session_log__athlete=s.athlete)
    assert (row.load, row.reps, row.rpe) == ("225", "5", "8")


@pytest.mark.django_db
def test_coach_new_line_defaults_to_a_set_once_started(client):
    # #709's strict default refused this text only because it read as 1 lb.
    s = seed()
    client.force_login(s.athlete)
    write_cell(client, s.session, s.squat, 1, "225 x 5")
    client.force_login(s.coach)
    resp = coach_write(client, s, "1x5, 225", line=2, intent="new")
    assert resp.status_code == 200
    cell = Prescription.objects.get(
        exercise_slot=s.squat.exercise_slot, week=s.week, line=2
    )
    assert cell.is_coach_set
    row = LoggedSet.objects.get(source_line=cell)
    assert (row.load, row.reps) == ("225", "5")
