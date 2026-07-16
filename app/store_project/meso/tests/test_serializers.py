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

from datetime import date
from types import SimpleNamespace

import pytest
from django.utils import timezone

from store_project.exercises.factories import ExerciseFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.models import Unit
from store_project.meso.models import Week
from store_project.meso.parsing import compose_prescription_text
from store_project.meso.serializers import serialize_athlete_identity
from store_project.meso.serializers import serialize_group_identity
from store_project.meso.serializers import serialize_mesocycle_grid
from store_project.meso.serializers import serialize_plan
from store_project.meso.serializers import serialize_plan_history

from ._helpers import day
from ._helpers import presc
from ._helpers import sub_line

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
        session = day(
            current_week,
            day_number=day_number,
            name=name,
            bias=bias,
            order=day_number,
        )
        for order, (ex_name, sets, reps, load, rpe, note, tags) in enumerate(exercises):
            presc(
                session,
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
        # ``id``/``index`` carry the real week pk so the switcher can target it.
        weeks = list(
            Week.objects.filter(mesocycle__plan=plan, mesocycle__order=1).order_by(
                "index"
            )
        )
        ids = [w.pk for w in weeks]
        assert result["weeks"] == [
            {
                "id": ids[0],
                "index": 1,
                "label": "Wk 1",
                "phase": "Accum",
                "vol": 70,
                "inten": 62,
                "deload": False,
                "current": False,
            },
            {
                "id": ids[1],
                "index": 2,
                "label": "Wk 2",
                "phase": "Accum",
                "vol": 85,
                "inten": 68,
                "deload": False,
                "current": True,
            },
            {
                "id": ids[2],
                "index": 3,
                "label": "Wk 3",
                "phase": "Accum",
                "vol": 100,
                "inten": 73,
                "deload": False,
                "current": False,
            },
            {
                "id": ids[3],
                "index": 4,
                "label": "Wk 4",
                "phase": "Deload",
                "vol": 55,
                "inten": 70,
                "deload": True,
                "current": False,
            },
        ]

    def test_serialize_plan_reports_the_viewed_week(self):
        plan = build_maya_plan()
        result = serialize_plan(plan)
        current = Week.objects.get(mesocycle__plan=plan, is_current=True)
        assert result["viewing"] == current.pk

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
                # Exactly the designer's keys (text-first, Phase 2a) — no
                # leakage of Phase 3/agent fields. tempo/rest/note are the
                # per-exercise slot columns (D2).
                expected_keys = {
                    "id",
                    "name",
                    "text",
                    "skipped",
                    "tempo",
                    "rest",
                    "note",
                    "lines",
                }
                if tags:
                    expected_keys.add("tag")
                assert set(ex.keys()) == expected_keys
                assert ex["skipped"] is False
                assert isinstance(ex["id"], int)
                assert ex["name"] == name
                # ``presc`` composes the old structured spec into freeform text.
                assert ex["text"] == compose_prescription_text(
                    sets=sets, reps=reps, rpe=rpe, load=load
                )
                assert ex["lines"] == []
                assert ex["tempo"] == ""
                assert ex["rest"] == ""
                assert ex["note"] == note
                if tags:
                    assert ex["tag"] == tags[0]
                # Derived in later slices, never emitted by Phase 2.
                assert "last" not in ex
                assert "adj" not in ex

    def test_phase_state_from_sequence_not_order_arithmetic(self):
        """`next` is the *adjacent* block by position, even with sparse orders.

        The model enforces unique — not contiguous — `order`, so reordering or
        deleting blocks can leave gaps (e.g. 0/10/20/30). The block right after
        the current one must still read `next`, not `future`.
        """
        rel = CoachAthleteFactory()
        plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
        sparse = [("Base", 0), ("Hypertrophy", 10), ("Strength", 20), ("Peak", 30)]
        mesos = [
            MesocycleFactory(plan=plan, name=name, order=order, week_count=4)
            for name, order in sparse
        ]
        WeekFactory(mesocycle=mesos[1], index=1, is_current=True)
        result = serialize_plan(plan)
        assert [p["state"] for p in result["phases"]] == [
            "done",
            "current",
            "next",
            "future",
        ]

    def test_program_picks_current_week_only(self):
        """`program` is the *current* week's sessions, not every week's."""
        plan = build_maya_plan()
        # A session on a non-current week must not appear in `program`.
        hypertrophy = plan.mesocycles.get(name="Hypertrophy")
        wk1 = hypertrophy.weeks.get(index=1)
        day(wk1, day_number=1, name="Should Not Appear", order=1)
        result = serialize_plan(plan)
        assert "Should Not Appear" not in [s["name"] for s in result["program"]]


class TestLastLoggedColumn:
    """Phase 3 — the designer's "last time" column lights up from real logs.

    ``serialize_plan`` adds a per-exercise ``last`` (a compact summary of the
    athlete's most recent logged sets for that exercise) so the coach sees what
    was actually done next to what they're prescribing. It is *absent* until a
    log exists — the no-log round-trip (``test_exercise_rows_round_trip``) still
    holds — and matches by exercise identity (name / catalog FK), so a prior
    week's log surfaces against the current week's same lift.
    """

    def _plan(self, *, sets="3", reps="6", load="70", rpe="7", unit=Unit.KILOGRAMS):
        rel = CoachAthleteFactory()
        plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE, unit=unit)
        meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
        week = WeekFactory(mesocycle=meso, index=2, is_current=True)
        session = day(week, day_number=1, name="Lower")
        cell = presc(
            session,
            name="Box Squat",
            order=0,
            sets=sets,
            reps=reps,
            load=load,
            rpe=rpe,
        )
        return SimpleNamespace(
            plan=plan,
            meso=meso,
            week=week,
            session=session,
            presc=cell,
            athlete=plan.athlete,
        )

    def _log(self, session, athlete, cell, *, when, sets):
        """Log ``sets`` (list of (reps, load, rpe)) against ``cell``."""
        log = SessionLogFactory(
            session=session,
            athlete=athlete,
            status=SessionLog.Status.DONE,
            date=when,
        )
        for n, (reps, load, rpe) in enumerate(sets, start=1):
            LoggedSetFactory(
                session_log=log,
                prescription=cell,
                set_number=n,
                reps=reps,
                load=load,
                rpe=rpe,
            )
        return log

    def _box_squat(self, plan):
        return serialize_plan(plan)["program"][0]["exercises"][0]

    def test_no_logs_no_last(self):
        s = self._plan()
        assert "last" not in self._box_squat(s.plan)

    def test_last_from_logged_sets(self):
        s = self._plan()
        self._log(
            s.session,
            s.athlete,
            s.presc,
            when=date(2026, 6, 24),
            sets=[("6", "70", "7"), ("6", "70", "7"), ("6", "70", "8")],
        )
        # Uniform reps/load collapse to the prototype's compact form; the badge
        # carries the hardest set's RPE.
        assert self._box_squat(s.plan)["last"] == "3×6 · 70kg · RPE8"

    def test_last_uses_plan_unit(self):
        s = self._plan(load="135", unit=Unit.POUNDS)
        self._log(
            s.session,
            s.athlete,
            s.presc,
            when=date(2026, 6, 24),
            sets=[("6", "135", "7")],
        )
        assert self._box_squat(s.plan)["last"] == "1×6 · 135lb · RPE7"

    def test_last_omits_unit_for_non_numeric_load(self):
        s = self._plan(load="BW")
        self._log(
            s.session,
            s.athlete,
            s.presc,
            when=date(2026, 6, 24),
            sets=[("12", "BW", "")],
        )
        # No RPE logged → no RPE segment; "BW" carries no unit suffix.
        assert self._box_squat(s.plan)["last"] == "1×12 · BW"

    def test_last_picks_most_recent_log(self):
        s = self._plan()
        self._log(
            s.session,
            s.athlete,
            s.presc,
            when=date(2026, 6, 10),
            sets=[("6", "60", "7")],
        )
        self._log(
            s.session,
            s.athlete,
            s.presc,
            when=date(2026, 6, 24),
            sets=[("6", "72.5", "7")],
        )
        assert self._box_squat(s.plan)["last"] == "1×6 · 72.5kg · RPE7"

    def test_last_matches_same_lift_across_weeks(self):
        """A prior week's log surfaces against the current week's same lift."""
        s = self._plan()
        wk1 = WeekFactory(mesocycle=s.meso, index=1, is_current=False)
        wk1_session = day(wk1, day_number=1, name="Lower")
        wk1_cell = presc(
            wk1_session,
            name="Box Squat",
            order=0,
            sets="3",
            reps="6",
            load="65",
            rpe="7",
        )
        self._log(
            wk1_session,
            s.athlete,
            wk1_cell,
            when=date(2026, 6, 17),
            sets=[("6", "65", "7")],
        )
        # The current (Wk 2) Box Squat shows last week's logged Box Squat.
        assert self._box_squat(s.plan)["last"] == "1×6 · 65kg · RPE7"

    def test_last_is_scoped_to_this_athlete(self):
        """Another athlete's log on a same-named lift never bleeds in."""
        s = self._plan()
        stranger = self._plan()  # different plan + athlete, also "Box Squat"
        self._log(
            stranger.session,
            stranger.athlete,
            stranger.presc,
            when=date(2026, 6, 24),
            sets=[("6", "200", "9")],
        )
        assert "last" not in self._box_squat(s.plan)


# ---------------------------------------------------------------------------
# serialize_mesocycle_grid — the P1 multi-week table (backend, dense grid)
# ---------------------------------------------------------------------------


def _build_grid_meso():
    """A 2-day, 3-row, 2-week block, fully wired for the P1 grid serializer.

    Day 1 (order 0): Back Squat (order 0, catalog-linked, tagged) + Leg Press
    (order 1). Day 2 (order 1): Bench Press (order 0). Every row carries a
    real cell in both weeks so density can be asserted directly.
    """
    rel = CoachAthleteFactory()
    plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0, week_count=4)
    week1 = WeekFactory(mesocycle=meso, index=1, phase="Accum", is_current=True)
    week2 = WeekFactory(
        mesocycle=meso,
        index=2,
        phase="Accum",
        is_current=False,
        delivered_at=timezone.now(),
    )

    day1 = day(week1, day_number=1, name="Lower", bias="Quad bias", order=0)
    day2 = day(week1, day_number=2, name="Upper", bias="Push/pull", order=1)
    # Block-shared identity (P0): week 2's days reuse the SAME SessionSlot.
    day1_wk2 = day(week2, session_slot=day1.session_slot)
    day2_wk2 = day(week2, session_slot=day2.session_slot)

    catalog = ExerciseFactory()
    squat_cell1 = presc(
        day1,
        name="Back Squat",
        order=0,
        exercise=catalog,
        tags=["main"],
        sets="4",
        reps="6",
        load="100",
        rpe="7",
        rest="120",
        note="tempo",
    )
    squat_row = squat_cell1.exercise_slot
    squat_cell2 = presc(
        exercise_slot=squat_row, week=week2, sets="4", reps="5", load="105", rpe="8"
    )

    press_cell1 = presc(day1, name="Leg Press", order=1, sets="3", reps="12", load="80")
    press_row = press_cell1.exercise_slot
    press_cell2 = presc(
        exercise_slot=press_row, week=week2, sets="3", reps="10", load="85"
    )

    bench_cell1 = presc(
        day2, name="Bench Press", order=0, sets="4", reps="8", load="60"
    )
    bench_row = bench_cell1.exercise_slot
    bench_cell2 = presc(
        exercise_slot=bench_row, week=week2, sets="4", reps="8", load="65"
    )

    return SimpleNamespace(
        plan=plan,
        meso=meso,
        week1=week1,
        week2=week2,
        day1=day1,
        day2=day2,
        day1_wk2=day1_wk2,
        day2_wk2=day2_wk2,
        squat_row=squat_row,
        press_row=press_row,
        bench_row=bench_row,
        squat_cell1=squat_cell1,
        squat_cell2=squat_cell2,
        press_cell1=press_cell1,
        press_cell2=press_cell2,
        bench_cell1=bench_cell1,
        bench_cell2=bench_cell2,
        catalog=catalog,
    )


class TestSerializeMesocycleGrid:
    def test_top_level_keys(self):
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        # Issue #455 phase A5: plan/group/athlete/phases join the grid payload
        # so DesignerRoot can retire the separate one-week `plan_data` owner
        # and hydrate the top bar / left rail / block view straight off the
        # grid (see this class's TestSerializeMesocycleGridIdentity below).
        assert set(result.keys()) == {
            "plan",
            "group",
            "athlete",
            "phases",
            "mesocycle",
            "weeks",
            "days",
            "history",
        }

    def test_mesocycle_envelope(self):
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        assert result["mesocycle"] == {
            "id": f.meso.pk,
            "plan_id": f.plan.pk,
            "name": "Hypertrophy",
            "week_count": 4,
        }

    def test_weeks_ordered_by_index_with_full_meta(self):
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        assert [w["id"] for w in result["weeks"]] == [f.week1.pk, f.week2.pk]
        assert result["weeks"][0] == {
            "id": f.week1.pk,
            "index": 1,
            "label": "Wk 1",
            "phase": "Accum",
            "deload": False,
            "current": True,
            "delivered_at": None,
            # Issue #455 phase A5: BlockView's periodization timeline bars
            # (barH(w.vol, ...)/barH(w.inten, ...)) need these on the grid
            # payload now that the one-week `plan_data`/`serialize_week` path
            # is no longer the front-end's only source for them.
            "vol": f.week1.volume,
            "inten": f.week1.intensity,
        }
        wk2 = result["weeks"][1]
        assert wk2["id"] == f.week2.pk
        assert wk2["index"] == 2
        assert wk2["label"] == "Wk 2"
        assert wk2["current"] is False
        assert wk2["delivered_at"] == f.week2.delivered_at.isoformat()
        assert wk2["vol"] == f.week2.volume
        assert wk2["inten"] == f.week2.intensity

    def test_days_ordered_with_session_id_and_identity(self):
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        assert [d["session_slot_id"] for d in result["days"]] == [
            f.day1.session_slot_id,
            f.day2.session_slot_id,
        ]
        day1_data = result["days"][0]
        assert day1_data["day_number"] == 1
        assert day1_data["name"] == "Lower"
        assert day1_data["bias"] == "Quad bias"
        assert day1_data["order"] == 0
        # Current week (week1) wins the session_id.
        assert day1_data["session_id"] == f.day1.pk

    def test_rows_ordered_by_order_with_block_identity(self):
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        day1_rows = result["days"][0]["rows"]
        assert [r["exercise_slot_id"] for r in day1_rows] == [
            f.squat_row.pk,
            f.press_row.pk,
        ]
        squat = day1_rows[0]
        assert squat["name"] == "Back Squat"
        assert squat["exercise_id"] == f.catalog.pk
        assert squat["order"] == 0
        assert squat["tags"] == ["main"]
        # Per-exercise columns (Phase 2a, D2) ride the ROW, not the cells.
        assert squat["tempo"] == ""
        assert squat["rest"] == "120"
        assert squat["note"] == "tempo"

    def test_cells_are_dense_one_per_live_week(self):
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        squat = result["days"][0]["rows"][0]
        assert set(squat["cells"].keys()) == {str(f.week1.pk), str(f.week2.pk)}

    def test_cell_carries_the_text_stack(self):
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        squat = result["days"][0]["rows"][0]
        cell = squat["cells"][str(f.week1.pk)]
        assert cell == {
            "prescription_id": f.squat_cell1.pk,
            "text": "4 x 6, RPE 7, 100",
            "skipped": False,
            "lines": [],
        }

    def test_deleted_week_is_excluded(self):
        f = _build_grid_meso()
        f.week2.soft_delete()
        result = serialize_mesocycle_grid(f.meso)
        assert [w["id"] for w in result["weeks"]] == [f.week1.pk]
        squat = result["days"][0]["rows"][0]
        assert set(squat["cells"].keys()) == {str(f.week1.pk)}

    def test_deleted_session_slot_is_excluded(self):
        f = _build_grid_meso()
        f.day2.session_slot.soft_delete()
        result = serialize_mesocycle_grid(f.meso)
        assert [d["session_slot_id"] for d in result["days"]] == [
            f.day1.session_slot_id
        ]

    def test_deleted_exercise_slot_is_excluded(self):
        f = _build_grid_meso()
        f.press_row.soft_delete()
        result = serialize_mesocycle_grid(f.meso)
        day1_rows = result["days"][0]["rows"]
        assert [r["exercise_slot_id"] for r in day1_rows] == [f.squat_row.pk]

    def test_skipped_cell_still_appears_flagged(self):
        f = _build_grid_meso()
        f.squat_cell2.skipped = True
        f.squat_cell2.save(update_fields=["skipped"])
        result = serialize_mesocycle_grid(f.meso)
        squat = result["days"][0]["rows"][0]
        cell = squat["cells"][str(f.week2.pk)]
        assert cell["skipped"] is True

    def test_substitution_sub_line_rides_the_cells_lines(self):
        # Phase 2a: a substitution is freeform sub-line text — the grid carries
        # it in the cell's ``lines`` stack, and the ROW identity stays block-wide.
        f = _build_grid_meso()
        sub = sub_line(f.squat_cell2, "Front Squat")
        result = serialize_mesocycle_grid(f.meso)
        squat = result["days"][0]["rows"][0]
        assert squat["name"] == "Back Squat"
        assert squat["cells"][str(f.week2.pk)]["lines"] == [
            {"id": sub.pk, "line": 1, "text": "Front Squat"}
        ]
        # The untouched week's stack is empty.
        assert squat["cells"][str(f.week1.pk)]["lines"] == []

    def test_blank_sub_line_is_kept_in_the_grid_stack(self):
        # Unlike athlete-facing serialization, the editor grid keeps a cleared
        # sub-line in place (blank cell, not a collapsed stack).
        f = _build_grid_meso()
        cleared = sub_line(f.squat_cell1, "")
        result = serialize_mesocycle_grid(f.meso)
        squat = result["days"][0]["rows"][0]
        assert squat["cells"][str(f.week1.pk)]["lines"] == [
            {"id": cleared.pk, "line": 1, "text": ""}
        ]

    def test_history_reuses_serialize_plan_history(self):
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        assert result["history"] == serialize_plan_history(f.plan)

    def test_session_id_falls_back_to_first_live_week_when_current_lacks_one(self):
        f = _build_grid_meso()
        # Simulate the current week's session for day1 having been individually
        # removed while the day (SessionSlot) itself stays live.
        Session.objects.filter(pk=f.day1.pk).update(deleted_at=timezone.now())
        result = serialize_mesocycle_grid(f.meso)
        day1_data = result["days"][0]
        assert day1_data["session_id"] == f.day1_wk2.pk

    def test_session_ids_maps_every_live_week_to_its_own_session_pk(self):
        # Codex #455 A2 review finding 2: a day-reorder client must be able
        # to look up ITS OWN current-week session id rather than trusting
        # the (possibly-fallback) ``session_id`` field.
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        day1_data = result["days"][0]
        assert day1_data["session_ids"] == {
            str(f.week1.pk): f.day1.pk,
            str(f.week2.pk): f.day1_wk2.pk,
        }
        day2_data = result["days"][1]
        assert day2_data["session_ids"] == {
            str(f.week1.pk): f.day2.pk,
            str(f.week2.pk): f.day2_wk2.pk,
        }

    def test_session_ids_omits_a_week_missing_a_live_session_even_though_session_id_falls_back(
        self,
    ):
        f = _build_grid_meso()
        # Same soft-delete as the session_id fallback test above: day1's
        # CURRENT week (week1) session is gone, so session_id falls back to
        # week2's — but session_ids must show NO entry for week1 at all
        # (never substitute the fallback), only the live week2 entry.
        Session.objects.filter(pk=f.day1.pk).update(deleted_at=timezone.now())
        result = serialize_mesocycle_grid(f.meso)
        day1_data = result["days"][0]
        assert (
            day1_data["session_id"] == f.day1_wk2.pk
        )  # display-only fallback, unaffected
        assert str(f.week1.pk) not in day1_data["session_ids"]
        assert day1_data["session_ids"] == {str(f.week2.pk): f.day1_wk2.pk}

    def test_individual_grid_cells_carry_no_adj_overlay(self):
        # The per-athlete ``adj`` overlay is a GROUP-only concern; an individual
        # plan's grid cells never carry ``adj``/``adjusts``.
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        for day_data in result["days"]:
            for row in day_data["rows"]:
                for cell in row["cells"].values():
                    assert "adj" not in cell
                    assert "adjusts" not in cell


class TestSerializeMesocycleGridIdentity:
    """Issue #455 phase A5: plan/group/athlete/phases join the grid payload.

    DesignerRoot's top bar / left rail / block view retire the separate
    one-week ``plan_data`` owner (``serialize_plan``) and hydrate straight off
    the grid now. These fields are additive re-uses of the exact helpers
    ``serialize_plan`` already calls (``serialize_group_identity``/
    ``serialize_athlete_identity``/``serialize_mesocycle``/``_phase_states``),
    just scoped to THIS grid's own mesocycle/plan rather than the plan's
    globally-current week.
    """

    def test_plan_summary(self):
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        assert result["plan"] == {
            "id": f.plan.pk,
            "title": f.plan.title,
            "goal": f.plan.goal,
            "status": f.plan.status,
            "unit": f.plan.unit,
        }

    def test_individual_plan_carries_athlete_and_no_group(self):
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        assert result["group"] is None
        assert result["athlete"] == serialize_athlete_identity(f.plan)
        assert result["athlete"]["name"]  # sanity: a real name, not blank

    def test_group_plan_carries_group_and_no_athlete(self):
        f = _build_group_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        assert result["athlete"] is None
        assert result["group"] == serialize_group_identity(f.group)
        assert result["group"]["name"] == f.group.name

    def test_phases_reflect_the_gridded_mesocycle_as_current(self):
        # ``_build_grid_meso`` creates a single mesocycle, so ``phases`` is
        # one "current" entry.
        f = _build_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        assert result["phases"] == [
            {"name": "Hypertrophy", "weeks": "4 wk", "state": "current"}
        ]

    def test_phases_are_scoped_to_the_grid_mesocycle_not_the_plans_viewed_week(self):
        # A later block, added after the gridded one: `serialize_mesocycle_
        # grid(meso)` must report ITS OWN mesocycle as "current" (mirrors P4's
        # validation-scoping precedent — the grid is its own block, not
        # necessarily whatever week the plan happens to be viewing).
        f = _build_grid_meso()
        MesocycleFactory(
            plan=f.plan, name="Strength", order=f.meso.order + 10, week_count=4
        )
        result = serialize_mesocycle_grid(f.meso)
        assert [p["state"] for p in result["phases"]] == ["current", "next"]


def _build_group_grid_meso():
    """A one-day, two-row, single-week GROUP block with one member override.

    Day 1 (order 0): Back Squat (order 0, load 100, overridden for the member)
    + Bench Press (order 1, no override). The member's adjust — a swap + a load %
    — is the ``adj`` overlay the grid must attach to the overridden cell only.
    """
    from store_project.meso.factories import GroupMembershipFactory
    from store_project.meso.factories import GroupPlanFactory
    from store_project.meso.factories import MesoGroupFactory

    group = MesoGroupFactory()
    plan = GroupPlanFactory(group=group, status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    day1 = day(week, day_number=1, name="Lower", order=0)
    squat_cell = presc(day1, name="Back Squat", order=0, load="100")
    bench_cell = presc(day1, name="Bench Press", order=1, load="60")
    membership = GroupMembershipFactory(group=group)
    membership.set_override(squat_cell, load_pct=90, swap_name="Box Squat")
    return SimpleNamespace(
        group=group,
        plan=plan,
        meso=meso,
        week=week,
        day1=day1,
        squat_cell=squat_cell,
        bench_cell=bench_cell,
        membership=membership,
    )


class TestSerializeMesocycleGridGroupAdj:
    """A group plan's grid cells carry the per-athlete ``adj`` overlay (P5).

    The multi-week table must show the same per-row adjust badge the single-week
    ``serialize_plan`` overlay does — driven by the members' real override diffs —
    so a coach editing the whole block still sees who diverges from the shared base.
    """

    def _cell(self, result, *, day_index, row_index, week):
        return result["days"][day_index]["rows"][row_index]["cells"][str(week.pk)]

    def test_overridden_cell_carries_adj_and_adjusts(self):
        f = _build_group_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        cell = self._cell(result, day_index=0, row_index=0, week=f.week)
        assert "adj" in cell
        assert "adjusts" in cell
        # The raw stored diff round-trips so the in-grid editor can pre-fill it.
        adjust = cell["adjusts"][0]
        assert adjust["swap"] == "Box Squat"
        assert adjust["load_pct"] == 90

    def test_unadjusted_cell_has_no_adj(self):
        f = _build_group_grid_meso()
        result = serialize_mesocycle_grid(f.meso)
        cell = self._cell(result, day_index=0, row_index=1, week=f.week)
        assert "adj" not in cell
        assert "adjusts" not in cell

    def test_dropped_member_leaves_no_adj(self):
        # ``group_adjustments`` scopes to *active* members — an ended link's adjust
        # drops off the grid, matching the single-week overlay.
        f = _build_group_grid_meso()
        f.membership.relationship.end()
        result = serialize_mesocycle_grid(f.meso)
        cell = self._cell(result, day_index=0, row_index=0, week=f.week)
        assert "adj" not in cell


class TestSerializeMesocycleGridQueries:
    """The grid stays a fixed number of queries — no N+1 over the block.

    Phase 2a: grid cells no longer carry ``one_rm`` (that A3 overlay — and its
    query — is gone with the structured %1RM ``load_type``), so the individual
    budget is the base grid queries plus A5's identity/phases pair:
    ``plan.mesocycles.all()`` and ``athlete.contraindications.all()``
    (``plan.athlete`` itself is an already-cached Python object here — the
    factories build plan/mesocycle from the same in-memory ``rel``).
    """

    def test_individual_grid_query_count(self, django_assert_num_queries):
        # 9 = weeks, session slots, sessions, exercise slots, cells,
        # 2x PlanAction (serialize_plan_history), mesocycles (phases),
        # contraindications (serialize_athlete_identity).
        f = _build_grid_meso()
        with django_assert_num_queries(9):
            serialize_mesocycle_grid(f.meso)

    def test_group_grid_query_count(self, django_assert_num_queries):
        # The group grid adds group_adjustments (1) plus serialize_group_
        # identity's active_member_users + one member's contraindications
        # (one active member in ``_build_group_grid_meso``) in place of the
        # individual path's single contraindications query.
        f = _build_group_grid_meso()
        with django_assert_num_queries(11):
            serialize_mesocycle_grid(f.meso)
