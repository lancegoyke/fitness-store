"""Round-trip: a seeded Plan serializes to the designer's expected JSON shape.

The Meso designer (``static/js/meso.js``) owns three in-memory state arrays —
``program`` (the current week's sessions + exercise rows), ``weeks`` (the current
mesocycle's week strip), and ``phases`` (the macrocycle rail). Phase 2's headline
deliverable is a ``serialize_plan`` that turns the real program schema
(``Plan → Mesocycle → Week → Session → ExercisePrescription``) into exactly that
shape, so Phase 3 can hydrate the designer from the DB instead of fixtures.

This test seeds Maya's hypertrophy block — the same data the prototype hard-codes
— and asserts the serializer reproduces the designer's shape. Fields that are
derived from *other* slices (``last`` from logged sets, ``adj`` from the agent)
are intentionally absent here; only the program-schema-owned fields round-trip.
"""

import pytest

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import Unit
from store_project.meso.serializers import serialize_plan

pytestmark = pytest.mark.django_db


# The macrocycle rail (meso.js `phases`). `week_count` is the *planned* length;
# only the current mesocycle materializes Week rows below.
MESOCYCLE_SPEC = [
    ("Base / GPP", 4),
    ("Hypertrophy", 4),
    ("Strength", 4),
    ("Peak / Test", 2),
]

# The current mesocycle's week strip (meso.js `weeks`).
# index, phase, volume, intensity, is_deload, is_current
WEEK_SPEC = [
    (1, "Accum", 70, 62, False, False),
    (2, "Accum", 85, 68, False, True),
    (3, "Accum", 100, 73, False, False),
    (4, "Deload", 55, 70, True, False),
]

# The current week's sessions (meso.js `program`). Each exercise carries the
# program-schema fields the designer renders; `tags` (list) maps to the
# designer's single `tag`. name, sets, reps, load, rpe, note, tags
SESSION_SPEC = [
    (
        "Lower",
        "Quad bias · knee-safe",
        [
            ("Box Squat (to parallel)", "4", "6", "70", "7", "", ["knee-safe"]),
            ("Bulgarian Split Squat (DB)", "3", "10", "18", "7", "", []),
            ("Leg Press (controlled ROM)", "3", "12", "110", "8", "", []),
            ("Seated Leg Curl", "3", "12", "41", "8", "", []),
            ("Standing Calf Raise", "4", "15", "60", "—", "", []),
        ],
    ),
    (
        "Upper",
        "Push / pull",
        [
            ("Incline DB Press", "4", "8", "24", "7", "monitor shoulder", []),
            ("Chest-Supported Row", "4", "10", "27", "7", "", []),
            ("Lat Pulldown", "3", "12", "52", "8", "", []),
            ("DB Shoulder Press", "3", "10", "16", "7", "neutral grip", []),
            ("Cable Lateral Raise", "3", "15", "9", "—", "", []),
        ],
    ),
    (
        "Posterior",
        "Hinge",
        [
            ("Trap-Bar Deadlift", "4", "6", "92.5", "7", "", []),
            ("Hip Thrust", "3", "10", "80", "8", "", []),
            ("Romanian Deadlift (3-1-1)", "3", "8", "60", "7", "tempo eccentric", []),
            (
                "Reverse Lunge (DB)",
                "3",
                "12",
                "14",
                "—",
                "knee-monitored",
                ["knee-safe"],
            ),
            ("Hanging Knee Raise", "3", "12", "BW", "—", "", []),
        ],
    ),
]


def build_maya_plan():
    """Seed the prototype's Maya hypertrophy block as real models."""
    rel = CoachAthleteFactory()
    plan = PlanFactory(
        relationship=rel,
        title="Hypertrophy Block",
        goal="Hypertrophy",
        unit=Unit.KILOGRAMS,
        status=Plan.Status.ACTIVE,
    )
    mesocycles = [
        MesocycleFactory(plan=plan, name=name, order=i, week_count=wc)
        for i, (name, wc) in enumerate(MESOCYCLE_SPEC)
    ]
    hypertrophy = mesocycles[1]
    weeks = [
        WeekFactory(
            mesocycle=hypertrophy,
            index=index,
            phase=phase,
            volume=vol,
            intensity=inten,
            is_deload=deload,
            is_current=current,
        )
        for (index, phase, vol, inten, deload, current) in WEEK_SPEC
    ]
    current_week = weeks[1]  # Wk 2
    for day_number, (name, bias, exercises) in enumerate(SESSION_SPEC, start=1):
        session = SessionFactory(
            week=current_week,
            day_number=day_number,
            name=name,
            bias=bias,
            order=day_number,
        )
        for order, (ex_name, sets, reps, load, rpe, note, tags) in enumerate(exercises):
            ExercisePrescriptionFactory(
                session=session,
                exercise=None,
                name=ex_name,
                order=order,
                sets=sets,
                reps=reps,
                load=load,
                rpe=rpe,
                note=note,
                tags=tags,
            )
    return plan


class TestSerializePlan:
    def test_plan_envelope(self):
        plan = build_maya_plan()
        result = serialize_plan(plan)
        assert result["plan"] == {
            "id": plan.pk,
            "title": "Hypertrophy Block",
            "goal": "Hypertrophy",
            "status": Plan.Status.ACTIVE,
            "unit": Unit.KILOGRAMS,
        }

    def test_phases_match_macrocycle(self):
        plan = build_maya_plan()
        result = serialize_plan(plan)
        assert result["phases"] == [
            {"name": "Base / GPP", "weeks": "4 wk", "state": "done"},
            {"name": "Hypertrophy", "weeks": "4 wk", "state": "current"},
            {"name": "Strength", "weeks": "4 wk", "state": "next"},
            {"name": "Peak / Test", "weeks": "2 wk", "state": "future"},
        ]

    def test_weeks_match_current_mesocycle(self):
        plan = build_maya_plan()
        result = serialize_plan(plan)
        assert result["weeks"] == [
            {
                "label": "Wk 1",
                "phase": "Accum",
                "vol": 70,
                "inten": 62,
                "deload": False,
                "current": False,
            },
            {
                "label": "Wk 2",
                "phase": "Accum",
                "vol": 85,
                "inten": 68,
                "deload": False,
                "current": True,
            },
            {
                "label": "Wk 3",
                "phase": "Accum",
                "vol": 100,
                "inten": 73,
                "deload": False,
                "current": False,
            },
            {
                "label": "Wk 4",
                "phase": "Deload",
                "vol": 55,
                "inten": 70,
                "deload": True,
                "current": False,
            },
        ]

    def test_program_is_current_weeks_sessions(self):
        plan = build_maya_plan()
        result = serialize_plan(plan)
        program = result["program"]
        assert [s["name"] for s in program] == ["Lower", "Upper", "Posterior"]
        assert [s["n"] for s in program] == [1, 2, 3]
        assert [s["bias"] for s in program] == [
            "Quad bias · knee-safe",
            "Push / pull",
            "Hinge",
        ]
        for session in program:
            assert isinstance(session["id"], int)

    def test_exercise_rows_round_trip(self):
        plan = build_maya_plan()
        result = serialize_plan(plan)
        for session, (_, _, ex_specs) in zip(result["program"], SESSION_SPEC):
            assert len(session["exercises"]) == len(ex_specs)
            for ex, (name, sets, reps, load, rpe, note, tags) in zip(
                session["exercises"], ex_specs
            ):
                # Exactly the designer's keys — no leakage of Phase 3/agent fields.
                expected_keys = {"id", "name", "sets", "reps", "load", "rpe", "note"}
                if tags:
                    expected_keys.add("tag")
                assert set(ex.keys()) == expected_keys
                assert isinstance(ex["id"], int)
                assert ex["name"] == name
                assert ex["sets"] == sets
                assert ex["reps"] == reps
                assert ex["load"] == load
                assert ex["rpe"] == rpe
                assert ex["note"] == note
                if tags:
                    assert ex["tag"] == tags[0]
                # Derived in later slices, never emitted by Phase 2.
                assert "last" not in ex
                assert "adj" not in ex

    def test_program_picks_current_week_only(self):
        """`program` is the *current* week's sessions, not every week's."""
        plan = build_maya_plan()
        # A session on a non-current week must not appear in `program`.
        hypertrophy = plan.mesocycles.get(name="Hypertrophy")
        wk1 = hypertrophy.weeks.get(index=1)
        SessionFactory(week=wk1, day_number=1, name="Should Not Appear", order=1)
        result = serialize_plan(plan)
        assert "Should Not Appear" not in [s["name"] for s in result["program"]]
