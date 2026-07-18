"""Seed the Meso coach-side demo: a coach, athletes, relationships, plans.

Phase 5 of the persistence slice (``docs/archive/meso/persistence-plan.md``) retires the
client-side mock for the coach-side screens. This command stands up the same
demo the prototype showed — **now real, DB-backed rows** — so a fresh dev
database renders the roster, athlete profile, and designer from actual data:

- a demo **coach** (you) with a ``CoachProfile`` (programming voice);
- five demo **athletes** (the prototype's Maya / Devon / Priya / Marcus / Lena)
  as ``User`` rows with ``AthleteProfile`` + global ``Contraindication`` rows;
- an **active** ``CoachAthlete`` link per athlete (coach-invited, accepted);
- **full programs for three of them** (Maya / Devon / Priya — Marcus and Lena
  stay plan-less) — every mesocycle block (Base/GPP → Hypertrophy → Strength →
  Peak/Test) built with a fixed lineup (``Mesocycle → SessionSlot →
  ExerciseSlot`` identity) **and** real per-week prescription text in every
  week (``Week → Prescription`` cells), reproducing the designer's fixture
  grid so ``serialize_plan`` round-trips it straight into the designer.

The command is **idempotent**: re-running ``get_or_create``s/``update_or_create``s
every row, so it never duplicates. ``--delete`` tears the demo back down (the
demo athletes and, by cascade, their links and plans) for a clean re-seed.
Every live week of every client's plan is delivered (2d: delivery no longer
gates visibility, so there's no "future, undelivered" distinction left to
model — see docs/meso/remove-current-week-plan.md §6); each plan also names a
"logged-through" week purely at the seed-data level (``logged_through_index``,
never a materialized field) — every week strictly before it gets a real,
multi-week logged training history, while the ones at/after it are built but
left unlogged. Maya's logged-through-week "Lower" session additionally carries
her original hand-authored log (``SAMPLE_LOG``) so the coach's results screen
and the designer's "last time" column light up off real data (athlete slice
Phase 3); the review screen renders real agent batches once a proposal is run.
"""

from datetime import date
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.core.management.base import CommandParser
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import get_random_string

from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Contraindication
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import LoggedSet
from store_project.meso.models import Mesocycle
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.models import SessionSlot
from store_project.meso.models import Unit
from store_project.meso.models import Week
from store_project.meso.one_rm import refresh_one_rms
from store_project.meso.parsing import compose_prescription_text
from store_project.meso.parsing import parse_prescription
from store_project.users.models import User

DEFAULT_COACH_EMAIL = "lancegoyke@gmail.com"

# The coach's programming voice (the prototype's COACH_STYLE).
COACH_STYLE_TAGS = [
    "Compound-first",
    "RPE-based load",
    "Free-weight bias",
    "2-min rest cap",
    "Unilateral work",
]
COACH_AVOID = (
    "machine-only days, untracked progressions, >3 exercises to failure / session."
)

# The five demo athletes (the prototype's roster). ``trained_months`` and
# ``age`` are stored as derived dates so the profile screen reads them back the
# way the mock did ("14 mo trained", "34"). Three (Maya/Devon/Priya) get a full
# program below (``PLANS``); Marcus and Lena stay plan-less on purpose (the
# roster's "no plan yet" state).
ATHLETES = [
    {
        "slug": "maya",
        "name": "Maya Okonkwo",
        "email": "maya.okonkwo@example.com",
        "age": 34,
        "trained_months": 14,
        "contraindications": [
            "L knee — avoid deep knee flexion under load",
            "No max-effort jumping / impact",
        ],
    },
    {
        "slug": "devon",
        "name": "Devon Reyes",
        "email": "devon.reyes@example.com",
        "age": 28,
        "trained_months": 6,
        "contraindications": ["R shoulder — neutral-grip pressing only"],
    },
    {
        "slug": "priya",
        "name": "Priya Nair",
        "email": "priya.nair@example.com",
        "age": 41,
        "trained_months": 72,
        "contraindications": [],
    },
    {
        "slug": "marcus",
        "name": "Marcus Tan",
        "email": "marcus.tan@example.com",
        "age": 35,
        "trained_months": 36,
        "contraindications": [],
    },
    {
        "slug": "lena",
        "name": "Lena Kovic",
        "email": "lena.kovic@example.com",
        "age": 31,
        "trained_months": 24,
        "contraindications": [
            "Lower back — trap-bar / RDL only, no conventional pull",
        ],
    },
]


# ---------------------------------------------------------------------------
# Progressive-week generator — turns a compact per-exercise "scheme" (starting
# sets/reps/RPE/load + a weekly load step) into real per-week cell text for
# every week of a block, so authoring three full programs stays a data
# problem, not a hand-typed-text problem. Only Maya's Hypertrophy block (the
# original fixture grid, preserved byte-for-byte on its logged-through week)
# skips this and stays hand-authored below.
# ---------------------------------------------------------------------------

# A phase's default sets/reps/RPE and where its working load sits relative to
# an exercise's authored ``ref_load`` (a demo-authored "moderately heavy"
# number, not a literal 1RM) — the classic base→hypertrophy→strength→peak
# rep-range/intensity progression, so the same lift lineup can be reused
# across a client's blocks with each block's own rep zone.
_PHASE_TABLE = {
    "base": {"sets": 3, "reps": 12, "rpe": 6.5, "pct": 0.55, "step_pct": 0.02},
    "hypertrophy": {"sets": 4, "reps": 9, "rpe": 7.5, "pct": 0.68, "step_pct": 0.02},
    "strength": {"sets": 4, "reps": 5, "rpe": 8, "pct": 0.82, "step_pct": 0.015},
    "peak": {"sets": 3, "reps": 3, "rpe": 8.5, "pct": 0.90, "step_pct": 0.02},
}

# Per-``kind`` starting/step volume+intensity for the block's ``Week`` columns
# (display-only strip metrics, not derived from the cells).
_BLOCK_TUNING = {
    "base": {
        "volume_start": 65,
        "volume_step": 8,
        "intensity_start": 55,
        "intensity_step": 3,
    },
    "hypertrophy": {
        "volume_start": 70,
        "volume_step": 10,
        "intensity_start": 62,
        "intensity_step": 4,
    },
    "strength": {
        "volume_start": 60,
        "volume_step": 6,
        "intensity_start": 75,
        "intensity_step": 4,
    },
    "peak": {
        "volume_start": 50,
        "volume_step": 10,
        "intensity_start": 85,
        "intensity_step": 4,
    },
}

_UNSET = object()


def _scheme_for_phase(
    ref_load, kind, *, sets=_UNSET, reps=_UNSET, rpe=_UNSET, load_pct=False
):
    """A per-exercise ``scheme`` dict for one training phase (``_PHASE_TABLE`` key).

    ``ref_load`` is a demo-authored "moderately heavy" working number (kg) for
    the lift — ``None`` for a bodyweight/unloaded move, which yields a
    constant ``"BW"`` load regardless of phase. ``sets``/``reps``/``rpe``
    override the phase's default for one exercise (pass ``rpe=None`` to force
    *no* RPE on an accessory row); ``load_pct`` marks a %1RM-notation row
    (Maya's Box Squat convention) rather than an absolute kg number.
    """
    table = _PHASE_TABLE[kind]
    resolved_sets = table["sets"] if sets is _UNSET else sets
    resolved_reps = table["reps"] if reps is _UNSET else reps
    if ref_load is None:
        return {
            "sets": resolved_sets,
            "reps": resolved_reps,
            "rpe": None if rpe is _UNSET else rpe,
            "load": "BW",
            "load_step": 0,
            "load_pct": False,
        }
    return {
        "sets": resolved_sets,
        "reps": resolved_reps,
        "rpe": table["rpe"] if rpe is _UNSET else rpe,
        "load": round(ref_load * table["pct"], 1),
        "load_step": round(ref_load * table["step_pct"], 2),
        "load_pct": load_pct,
    }


def _step_load(load, step, n):
    """``load`` advanced by ``step`` × ``n`` weeks, as canonical cell text.

    A non-numeric load (``"BW"``) is held constant — bodyweight doesn't
    progress by adding plates. Whole numbers drop their trailing ``.0``
    (``"70"``, not ``"70.0"``), matching the coach's own notation.
    """
    if load is None:
        return None
    try:
        value = round(float(load) + step * n, 1)
    except (TypeError, ValueError):
        return load
    if value == int(value):
        value = int(value)
    return str(value)


def _ease_rpe(rpe):
    """A deload week's RPE — ~1.5 easier than the work-week target, floored at 5."""
    value = max(5.0, float(rpe) - 1.5)
    if value == int(value):
        return str(int(value))
    return str(value)


def _week_cell(scheme, week_index, *, deload_index=None):
    """One exercise's cell (``{"text": ...}``) for one week of its block.

    ``week_index`` is 1-based within the block; a deload week (``week_index
    == deload_index``) trims a set, eases RPE, and resets load back to the
    block's starting point rather than tapering off an already-progressed
    number. ``scheme`` absent (``None``/``{}``) yields a blank cell.
    """
    if not scheme:
        return {}
    is_deload = deload_index is not None and week_index == deload_index
    sets = scheme["sets"]
    reps = scheme["reps"]
    rpe = scheme.get("rpe")
    load = scheme.get("load")
    step = scheme.get("load_step", 0)
    if is_deload:
        sets = max(2, sets - 1)
        load = _step_load(load, step, 0)
        if rpe is not None:
            rpe = _ease_rpe(rpe)
    else:
        load = _step_load(load, step, week_index - 1)
    return {
        "text": compose_prescription_text(
            sets=sets,
            reps=reps,
            rpe="" if rpe is None else rpe,
            load="" if load is None else load,
            load_pct=scheme.get("load_pct", False),
        )
    }


def _cells_for_week(days, week_index, *, deload_index=None):
    """The block's full ``{day_number: [cell, ...]}`` grid for one week."""
    return {
        day["day_number"]: [
            _week_cell(ex.get("scheme"), week_index, deload_index=deload_index)
            for ex in day["exercises"]
        ]
        for day in days
    }


def _progressive_weeks(
    days,
    *,
    count,
    phase,
    deload_index=None,
    start_index=1,
    volume_start=60,
    volume_step=8,
    intensity_start=60,
    intensity_step=4,
    phase_overrides=None,
):
    """Auto-generate ``count`` ``Week`` specs (index/phase/…/cells) for a block.

    ``phase_overrides`` (``{index: label}``) renames one non-deload week's
    phase label (e.g. Peak/Test's week 2 reading "Test" instead of "Peak").
    """
    phase_overrides = phase_overrides or {}
    weeks = []
    for offset in range(count):
        index = start_index + offset
        is_deload = deload_index is not None and index == deload_index
        if is_deload:
            volume = max(35, volume_start - 15)
            intensity = min(95, intensity_start + intensity_step)
            week_phase = "Deload"
        else:
            volume = min(100, volume_start + volume_step * offset)
            intensity = min(95, intensity_start + intensity_step * offset)
            week_phase = phase_overrides.get(index, phase)
        weeks.append(
            {
                "index": index,
                "phase": week_phase,
                "volume": volume,
                "intensity": intensity,
                "is_deload": is_deload,
                "cells": _cells_for_week(days, index, deload_index=deload_index),
            }
        )
    return weeks


def _ex(
    name, ref_load, kind, *, rest="90s", tags=None, note="", tempo="", **scheme_kwargs
):
    """One ``ExerciseSlot`` row (identity + per-exercise columns) + its scheme."""
    entry = {"name": name, "rest": rest}
    if tags:
        entry["tags"] = tags
    if note:
        entry["note"] = note
    if tempo:
        entry["tempo"] = tempo
    entry["scheme"] = _scheme_for_phase(ref_load, kind, **scheme_kwargs)
    return entry


def _client_days(day_templates, kind):
    """Materialize one phase's ``"days"`` lineup from compact templates.

    ``day_templates`` = ``[(day_number, name, bias, [(ex_name, ref_load,
    kwargs), ...]), ...]`` — the same lineup reused across a client's blocks,
    one call per block's ``kind`` (phase), so the exercise selection is
    authored once and the rep/load zone changes per block.
    """
    return [
        {
            "day_number": day_number,
            "name": name,
            "bias": bias,
            "exercises": [
                _ex(ex_name, ref_load, kind, **kw)
                for ex_name, ref_load, kw in exercises
            ],
        }
        for day_number, name, bias, exercises in day_templates
    ]


def _block(
    name,
    order,
    days,
    *,
    kind,
    count,
    phase,
    deload_index=None,
    logged_through_index=None,
    phase_overrides=None,
):
    """One ``SAMPLE_PLAN``-mesocycle-shaped block spec, fully built.

    ``logged_through_index`` is a plain seed-data bookkeeping value — how deep
    into THIS block ``_log_plan_history`` should generate a logged training
    history — never a materialized ``Week`` field (see that method's
    docstring). At most one block across a plan sets it.
    """
    tuning = _BLOCK_TUNING[kind]
    return {
        "name": name,
        "order": order,
        "week_count": count,
        "days": days,
        "weeks": _progressive_weeks(
            days,
            count=count,
            phase=phase,
            deload_index=deload_index,
            phase_overrides=phase_overrides,
            **tuning,
        ),
        "logged_through_index": logged_through_index,
    }


def _client_block(
    name,
    order,
    day_templates,
    *,
    kind,
    count,
    phase,
    deload_index=None,
    logged_through_index=None,
    phase_overrides=None,
):
    """``_block`` from a client's day templates for one phase ``kind``."""
    return _block(
        name,
        order,
        _client_days(day_templates, kind),
        kind=kind,
        count=count,
        phase=phase,
        deload_index=deload_index,
        logged_through_index=logged_through_index,
        phase_overrides=phase_overrides,
    )


# ---------------------------------------------------------------------------
# Maya's plan — the designer's original fixture grid. P0 fixed-lineup shape:
# each mesocycle's ``"days"`` is the block's fixed lineup — a ``SessionSlot``
# (day) per entry, each with an ordered ``"exercises"`` list — an
# ``ExerciseSlot`` (row) per entry — expressed **once per block**, since
# identity (name/bias/tags/catalog link/order) is shared across every week;
# ``tempo``/``rest``/``note`` (the per-exercise columns, Phase 2a / D2) ride
# each exercise entry. ``"weeks"`` are the block's ``Week`` columns; a week
# that carries a ``"cells"`` dict sets its freeform cell text —
# ``{day_number: [<cell>, ...]}``, one dict per row in that day's
# ``"exercises"`` order, each ``{"text": "<freeform>", "skipped": bool,
# "lines": ["<sub-line>", ...]}`` (all optional).
#
# Her Hypertrophy block's **logged-through week (index 2) is preserved
# verbatim** — many tests assert its exact cell text — while weeks 1/3/4
# (previously blank)
# now carry real progressive text generated from the same per-exercise
# ``scheme``s (added as an extra, build_block-ignored key on each exercise
# entry below). Her other three blocks (Base/GPP, Strength, Peak/Test) are
# newly built in full via the generic generator above, respecting her L-knee /
# no-impact contraindications throughout.
# ---------------------------------------------------------------------------

_HYPERTROPHY_DAYS = [
    {
        "day_number": 1,
        "name": "Lower",
        "bias": "Quad bias · knee-safe",
        "exercises": [
            # Prescribed as a % of 1RM in its cell text — the demo row that
            # shows percent notation parsing.
            {
                "name": "Box Squat (to parallel)",
                "tags": ["knee-safe"],
                "rest": "2 min",
                "scheme": {
                    "sets": 4,
                    "reps": 6,
                    "rpe": 7,
                    "load": 70,
                    "load_step": 1,
                    "load_pct": True,
                },
            },
            {
                "name": "Bulgarian Split Squat (DB)",
                "rest": "90s",
                "scheme": {"sets": 3, "reps": 10, "rpe": 7, "load": 16, "load_step": 2},
            },
            {
                "name": "Leg Press (controlled ROM)",
                "rest": "90s",
                "scheme": {
                    "sets": 3,
                    "reps": 12,
                    "rpe": 8,
                    "load": 102,
                    "load_step": 4,
                },
            },
            {
                "name": "Seated Leg Curl",
                "rest": "60s",
                "scheme": {"sets": 3, "reps": 12, "rpe": 8, "load": 37, "load_step": 2},
            },
            {
                "name": "Standing Calf Raise",
                "rest": "45s",
                "scheme": {
                    "sets": 4,
                    "reps": 15,
                    "rpe": None,
                    "load": 55,
                    "load_step": 2.5,
                },
            },
        ],
    },
    {
        "day_number": 2,
        "name": "Upper",
        "bias": "Push / pull",
        "exercises": [
            {
                "name": "Incline DB Press",
                "rest": "2 min",
                "note": "monitor shoulder",
                "scheme": {"sets": 4, "reps": 8, "rpe": 7, "load": 22, "load_step": 1},
            },
            {
                "name": "Chest-Supported Row",
                "rest": "90s",
                "scheme": {"sets": 4, "reps": 10, "rpe": 7, "load": 25, "load_step": 1},
            },
            {
                "name": "Lat Pulldown",
                "rest": "75s",
                "scheme": {"sets": 3, "reps": 12, "rpe": 8, "load": 48, "load_step": 2},
            },
            {
                "name": "DB Shoulder Press",
                "rest": "90s",
                "note": "neutral grip",
                "scheme": {"sets": 3, "reps": 10, "rpe": 7, "load": 14, "load_step": 1},
            },
            {
                "name": "Cable Lateral Raise",
                "rest": "60s",
                "scheme": {
                    "sets": 3,
                    "reps": 12,
                    "rpe": 8,
                    "load": 9,
                    "load_step": 0.5,
                },
            },
        ],
    },
    {
        "day_number": 3,
        "name": "Posterior",
        "bias": "Hinge",
        "exercises": [
            {
                "name": "Trap-Bar Deadlift",
                "rest": "3 min",
                "scheme": {
                    "sets": 4,
                    "reps": 6,
                    "rpe": 7,
                    "load": 85,
                    "load_step": 3.5,
                },
            },
            {
                "name": "Hip Thrust",
                "rest": "2 min",
                "tempo": "311",
                "scheme": {
                    "sets": 3,
                    "reps": 10,
                    "rpe": 8,
                    "load": 73,
                    "load_step": 3.5,
                },
            },
            {
                "name": "Romanian Deadlift (3-1-1)",
                "rest": "90s",
                "note": "tempo eccentric",
                "scheme": {"sets": 3, "reps": 8, "rpe": 7, "load": 54, "load_step": 3},
            },
            {
                "name": "Reverse Lunge (DB)",
                "tags": ["knee-safe"],
                "rest": "60s",
                "note": "knee-monitored",
                "scheme": {
                    "sets": 3,
                    "reps": 12,
                    "rpe": None,
                    "load": 12,
                    "load_step": 1,
                },
            },
            {
                "name": "Hanging Knee Raise",
                "rest": "45s",
                "scheme": {
                    "sets": 3,
                    "reps": 12,
                    "rpe": None,
                    "load": "BW",
                    "load_step": 0,
                },
            },
        ],
    },
]

# Maya's Base/GPP lineup — deliberately DIFFERENT movement names than her
# Hypertrophy block (general-prep substitutes, not the same lifts at a lighter
# load): Base/GPP is fully logged history (it's entirely before her
# logged-through week), so reusing a Hypertrophy lift name here would feed
# those lighter prep-phase loads into the *same* derived-1RM identity as her
# real Hypertrophy logs and drag it down — separate names keep the two
# blocks' histories from colliding. Still fully knee-safe / no-impact
# throughout.
_MAYA_PREP_DAY_TEMPLATES = [
    (
        1,
        "Lower Prep",
        "General prep · knee-safe",
        [
            ("Goblet Squat (box)", 32, {"tags": ["knee-safe"], "rest": "2 min"}),
            ("Step-Up (low box)", 14, {"tags": ["knee-safe"], "rest": "90s"}),
            (
                "Machine Leg Extension (partial ROM)",
                45,
                {"tags": ["knee-safe"], "rest": "60s"},
            ),
            ("Prone Hamstring Curl", 30, {"rest": "60s"}),
            ("Seated Calf Raise", 40, {"rest": "45s", "rpe": None}),
        ],
    ),
    (
        2,
        "Upper Prep",
        "General prep",
        [
            ("Flat DB Press", 20, {"rest": "2 min"}),
            ("Seated Cable Row", 40, {"rest": "90s"}),
            ("Assisted Pull-Up", 20, {"rest": "75s"}),
            ("Half-Kneeling DB Press", 12, {"rest": "90s"}),
            ("Band Pull-Apart", None, {"rest": "45s", "rpe": None}),
        ],
    ),
    (
        3,
        "Posterior Prep",
        "Hinge · knee-safe",
        [
            ("Glute Bridge (bilateral)", 50, {"tags": ["knee-safe"], "rest": "2 min"}),
            ("Cable Pull-Through", 35, {"rest": "90s"}),
            ("Back Extension", None, {"rest": "60s", "rpe": None}),
            ("Side-Lying Hip Abduction", 8, {"rest": "45s"}),
            ("Dead Bug (loaded)", None, {"rest": "45s", "rpe": None}),
        ],
    ),
]

# Maya's Strength / Peak-Test lineup — her Hypertrophy block's own lifts,
# reused at a heavier/lower-rep zone. Safe to share names with the Hypertrophy
# block (unlike Base/GPP above): both these blocks fall AFTER her
# logged-through week, so neither is ever logged — no history to collide with.
_MAYA_MAIN_DAY_TEMPLATES = [
    (
        1,
        "Lower",
        "Quad bias · knee-safe",
        [
            (
                "Box Squat (to parallel)",
                100,
                {"tags": ["knee-safe"], "rest": "2 min", "load_pct": True},
            ),
            ("Bulgarian Split Squat (DB)", 20, {"rest": "90s"}),
            ("Leg Press (controlled ROM)", 115, {"rest": "90s"}),
            ("Seated Leg Curl", 42, {"rest": "60s"}),
            ("Standing Calf Raise", 60, {"rest": "45s", "rpe": None}),
        ],
    ),
    (
        2,
        "Upper",
        "Push / pull",
        [
            ("Incline DB Press", 25, {"rest": "2 min"}),
            ("Chest-Supported Row", 28, {"rest": "90s"}),
            ("Lat Pulldown", 52, {"rest": "75s"}),
            ("DB Shoulder Press", 16, {"rest": "90s"}),
            ("Cable Lateral Raise", 10, {"rest": "60s", "rpe": None}),
        ],
    ),
    (
        3,
        "Posterior",
        "Hinge",
        [
            ("Trap-Bar Deadlift", 95, {"rest": "3 min"}),
            ("Hip Thrust", 80, {"tempo": "311", "rest": "2 min"}),
            ("Romanian Deadlift (3-1-1)", 60, {"rest": "90s"}),
            ("Reverse Lunge (DB)", 14, {"tags": ["knee-safe"], "rest": "60s"}),
            ("Hanging Knee Raise", None, {"rest": "45s", "rpe": None}),
        ],
    ),
]

# Maya's Hypertrophy "weeks" — index/phase/volume/intensity/is_deload are
# UNCHANGED from the original fixture; week 2 (the plan's logged-through
# week — see SAMPLE_PLAN's ``logged_through_index`` below) keeps its exact
# original ``cells`` dict verbatim (the Box Squat ``72%`` row, the
# ``{"skipped": True}`` cell, the ``Cable Crunch`` sub-line) — many tests
# assert this precisely. Weeks 1/3/4 gain generated ``cells`` (previously
# blank).
_HYPERTROPHY_WEEKS = [
    {
        "index": 1,
        "phase": "Accum",
        "volume": 70,
        "intensity": 62,
        "is_deload": False,
        "cells": _cells_for_week(_HYPERTROPHY_DAYS, 1, deload_index=4),
    },
    {
        "index": 2,
        "phase": "Accum",
        "volume": 85,
        "intensity": 68,
        "is_deload": False,
        "cells": {
            1: [
                {"text": "4 x 6, RPE 7, 72%"},
                {"text": "3 x 10, RPE 7, 18"},
                {"text": "3 x 12, RPE 8, 110"},
                {"text": "3 x 12, RPE 8, 41"},
                {"text": "4 x 15, 60"},
            ],
            2: [
                {"text": "4 x 8, RPE 7, 24"},
                {"text": "4 x 10, RPE 7, 27"},
                {"text": "3 x 12, RPE 8, 52"},
                {"text": "3 x 10, RPE 7, 16"},
                # A one-week exception: shoulder felt off, so this row is
                # skipped for Wk 2 only (the em-dash cell) — not logged, so
                # it's safe to demo here.
                {"skipped": True},
            ],
            3: [
                {"text": "4 x 6, RPE 7, 92.5"},
                {"text": "3 x 10, RPE 8, 80"},
                {"text": "3 x 8, RPE 7, 60"},
                {"text": "3 x 12, 14"},
                # A one-week substitution, freeform-style (§2.6): the
                # substitute movement typed into a sub-line (block identity
                # stays "Hanging Knee Raise").
                {"text": "3 x 12, BW", "lines": ["Cable Crunch"]},
            ],
        },
    },
    {
        "index": 3,
        "phase": "Accum",
        "volume": 100,
        "intensity": 73,
        "is_deload": False,
        "cells": _cells_for_week(_HYPERTROPHY_DAYS, 3, deload_index=4),
    },
    {
        "index": 4,
        "phase": "Deload",
        "volume": 55,
        "intensity": 70,
        "is_deload": True,
        "cells": _cells_for_week(_HYPERTROPHY_DAYS, 4, deload_index=4),
    },
]

SAMPLE_PLAN = {
    "title": "Hypertrophy Block",
    "goal": "Hypertrophy",
    "mesocycles": [
        _client_block(
            "Base / GPP",
            0,
            _MAYA_PREP_DAY_TEMPLATES,
            kind="base",
            count=4,
            phase="Prep",
            deload_index=4,
        ),
        {
            "name": "Hypertrophy",
            "order": 1,
            "week_count": 4,
            "days": _HYPERTROPHY_DAYS,
            "weeks": _HYPERTROPHY_WEEKS,
            # Seed-data-only bookkeeping (never a materialized field) — see
            # ``_block``'s docstring and ``_log_plan_history``.
            "logged_through_index": 2,
        },
        _client_block(
            "Strength",
            2,
            _MAYA_MAIN_DAY_TEMPLATES,
            kind="strength",
            count=4,
            phase="Int",
            deload_index=4,
        ),
        _client_block(
            "Peak / Test",
            3,
            _MAYA_MAIN_DAY_TEMPLATES,
            kind="peak",
            count=2,
            phase="Peak",
            phase_overrides={2: "Test"},
        ),
    ],
}

# Maya's logged "Lower" session (the logged-through week, Day 1) — the first real logged
# rows on the demo. Worked mostly to target, with the Box Squat top set running
# hot and the last leg-curl set falling short, so the results screen shows a real
# completion %, an RPE-over flag, and a shortfall note. ``(reps, load, rpe)`` per
# set, keyed by the prescription's name.
SAMPLE_LOG = {
    "mesocycle": "Hypertrophy",
    "week_index": 2,
    "day_number": 1,
    "logged_days_ago": 2,
    "sets": {
        "Box Squat (to parallel)": [
            ("6", "70", "7"),
            ("6", "70", "7"),
            ("6", "70", "7"),
            ("6", "70", "8.5"),
        ],
        "Bulgarian Split Squat (DB)": [("10", "18", "7")] * 3,
        "Leg Press (controlled ROM)": [("12", "110", "8")] * 3,
        "Seated Leg Curl": [("12", "41", "8"), ("12", "41", "8"), ("9", "41", "8.5")],
        "Standing Calf Raise": [("15", "60", "")] * 4,
    },
}


# ---------------------------------------------------------------------------
# Devon's plan — R shoulder, neutral-grip pressing only (6 months trained:
# still building his base). Every lift is either neutral-grip or has no
# shoulder-pressing component at all. Logged-through week: Strength block,
# week 2 of 4 — Base/GPP + Hypertrophy fully behind him (logged history),
# Strength wk1 behind him too, Strength wk3/4 + Peak/Test still ahead (built,
# but unlogged).
# ---------------------------------------------------------------------------

_DEVON_DAY_TEMPLATES = [
    (
        1,
        "Push (neutral-grip)",
        "Press · shoulder-safe",
        [
            (
                "Neutral-Grip DB Bench Press",
                26,
                {"tags": ["shoulder-safe"], "rest": "2 min"},
            ),
            (
                "Landmine Press (single-arm)",
                20,
                {"rest": "90s", "note": "neutral grip"},
            ),
            (
                "Seated DB Shoulder Press (neutral grip)",
                16,
                {"rest": "90s", "note": "neutral grip"},
            ),
            ("Cable Tricep Pushdown", 24, {"rest": "60s", "rpe": None}),
        ],
    ),
    (
        2,
        "Pull",
        "Row / pulldown",
        [
            ("Neutral-Grip Lat Pulldown", 48, {"rest": "2 min"}),
            ("Chest-Supported Row (neutral grip)", 28, {"rest": "90s"}),
            ("Face Pull", 16, {"rest": "60s", "note": "shoulder health", "rpe": None}),
            ("DB Curl", 12, {"rest": "60s"}),
        ],
    ),
    (
        3,
        "Legs",
        "Squat / hinge",
        [
            ("Goblet Squat", 26, {"rest": "2 min"}),
            ("Romanian Deadlift", 50, {"rest": "2 min"}),
            ("Walking Lunge (DB)", 14, {"rest": "90s"}),
            ("Leg Curl", 30, {"rest": "60s"}),
            ("Standing Calf Raise", 46, {"rest": "45s", "rpe": None}),
        ],
    ),
]

DEVON_PLAN = {
    "title": "Shoulder-Smart Strength",
    "goal": "General Strength",
    "mesocycles": [
        _client_block(
            "Base / GPP",
            0,
            _DEVON_DAY_TEMPLATES,
            kind="base",
            count=4,
            phase="Prep",
            deload_index=4,
        ),
        _client_block(
            "Hypertrophy",
            1,
            _DEVON_DAY_TEMPLATES,
            kind="hypertrophy",
            count=4,
            phase="Accum",
            deload_index=4,
        ),
        _client_block(
            "Strength",
            2,
            _DEVON_DAY_TEMPLATES,
            kind="strength",
            count=4,
            phase="Int",
            deload_index=4,
            logged_through_index=2,
        ),
        _client_block(
            "Peak / Test",
            3,
            _DEVON_DAY_TEMPLATES,
            kind="peak",
            count=2,
            phase="Peak",
            phase_overrides={2: "Test"},
        ),
    ],
}


# ---------------------------------------------------------------------------
# Priya's plan — no contraindications, 72 months trained: an advanced,
# heavier barbell-first program. Logged-through week: Hypertrophy block, week
# 3 of 4 — Base/GPP fully behind her plus Hypertrophy wk1/2 (logged history);
# Hypertrophy wk4 (deload) + Strength + Peak/Test still ahead (built, but
# unlogged) — two whole future blocks past her logged-through one.
# ---------------------------------------------------------------------------

_PRIYA_DAY_TEMPLATES = [
    (
        1,
        "Squat Day",
        "Squat + accessories",
        [
            ("Back Squat", 145, {"rest": "3 min"}),
            ("Front Squat", 100, {"rest": "2 min"}),
            ("Barbell Walking Lunge", 60, {"rest": "90s"}),
            ("Leg Curl", 55, {"rest": "60s"}),
            ("Standing Calf Raise", 90, {"rest": "45s", "rpe": None}),
        ],
    ),
    (
        2,
        "Bench Day",
        "Press + pull",
        [
            ("Bench Press", 100, {"rest": "3 min"}),
            ("Weighted Pull-Up", 25, {"rest": "2 min"}),
            ("Overhead Press", 55, {"rest": "2 min"}),
            ("Barbell Row", 80, {"rest": "90s"}),
            ("Face Pull", 20, {"rest": "60s", "rpe": None}),
        ],
    ),
    (
        3,
        "Deadlift Day",
        "Hinge + posterior chain",
        [
            ("Conventional Deadlift", 165, {"rest": "3 min"}),
            ("Romanian Deadlift", 115, {"rest": "2 min"}),
            ("Hip Thrust", 130, {"rest": "90s"}),
            ("Glute Ham Raise", None, {"rest": "60s"}),
            ("Hanging Leg Raise", None, {"rest": "45s", "rpe": None}),
        ],
    ),
]

PRIYA_PLAN = {
    "title": "Advanced Strength Cycle",
    "goal": "Strength",
    "mesocycles": [
        _client_block(
            "Base / GPP",
            0,
            _PRIYA_DAY_TEMPLATES,
            kind="base",
            count=4,
            phase="Prep",
            deload_index=4,
        ),
        _client_block(
            "Hypertrophy",
            1,
            _PRIYA_DAY_TEMPLATES,
            kind="hypertrophy",
            count=4,
            phase="Accum",
            deload_index=4,
            logged_through_index=3,
        ),
        _client_block(
            "Strength",
            2,
            _PRIYA_DAY_TEMPLATES,
            kind="strength",
            count=4,
            phase="Int",
            deload_index=4,
        ),
        _client_block(
            "Peak / Test",
            3,
            _PRIYA_DAY_TEMPLATES,
            kind="peak",
            count=2,
            phase="Peak",
            phase_overrides={2: "Test"},
        ),
    ],
}

#: Athlete slug → plan spec, for the three clients with a full program.
#: Marcus and Lena (in ``ATHLETES`` but not here) stay plan-less.
PLANS = {
    "maya": SAMPLE_PLAN,
    "devon": DEVON_PLAN,
    "priya": PRIYA_PLAN,
}


# A pending email invite (N4) so the roster's onboarding surface is visible — a
# person the coach invited who hasn't claimed an account yet.
PENDING_INVITE_EMAIL = "prospect@example.com"

# A pending athlete→coach request (N4 Phase 2) so the roster's request surface is
# visible — an existing user who has asked to train under the coach.
PENDING_REQUEST_EMAIL = "hopeful@example.com"
PENDING_REQUEST_NAME = "Hopeful Newcomer"

# A former athlete (an ENDED link) so the relationship-history surface ("Past
# athletes", re-invitable) is visible on a fresh DB.
PAST_ATHLETE_EMAIL = "alum@example.com"
PAST_ATHLETE_NAME = "Jordan Alumni"


def _months_before(today, months):
    """The date ``months`` whole months before ``today`` (day clamped to ≤28)."""
    total = today.year * 12 + (today.month - 1) - months
    year, month = divmod(total, 12)
    return date(year, month + 1, min(today.day, 28))


def _years_before(today, years):
    """The date ``years`` years before ``today`` (day clamped to ≤28)."""
    return date(today.year - years, today.month, min(today.day, 28))


def build_block(mesocycle, block_spec):
    """Materialize one mesocycle's fixed lineup + weeks + per-week cells.

    The shared tree-builder behind both demo seeders (``seed_meso_demo`` and
    ``demo.py``'s coach-scoped one-click demo) — collapses what used to be two
    near-identical ``Mesocycle → Week → Session → ExercisePrescription``
    builders into one, for the P0 fixed-lineup shape.

    ``block_spec`` is a ``SAMPLE_PLAN``-mesocycle-shaped dict:

    - ``"days"``: the block's fixed lineup, expressed **once** — each entry is
      a ``SessionSlot`` (``day_number``/``name``/``bias``/``order``) with an
      ordered ``"exercises"`` list, each an ``ExerciseSlot`` row
      (``name``/``exercise``/``tags`` plus the per-exercise ``tempo``/``rest``/
      ``note`` columns, Phase 2a / D2);
    - ``"weeks"``: the block's ``Week`` columns (``index``/``phase``/``volume``/
      ``intensity``/``is_deload``). EVERY listed week materializes
      the full fixed lineup — a ``Session`` per day and a line-0 ``Prescription``
      cell per row (invariant: every slot × live-week has a cell) — so the block
      is dense. A week may carry a ``"cells"`` dict (``{day_number: [<cell>,
      ...]}``, one dict per row in that day's ``"exercises"`` order) setting each
      cell's freeform ``"text"``, the one-week ``"skipped"`` exception, and
      optional ``"lines"`` (sub-line text strings, materialized at line 1+); a
      week without one gets blank cells (the lineup, no text). Blocks with no
      ``"weeks"`` stay planned-length-only (``week_count``, no ``Week`` rows).

    Idempotent on the P0 natural keys — ``SessionSlot`` by ``(mesocycle,
    day_number)``, ``ExerciseSlot`` by ``(session_slot, order)``, ``Week`` by
    ``(mesocycle, index)``, ``Prescription`` cell by ``(exercise_slot, week,
    line)`` — so re-running a seeder never duplicates rows even if called more
    than once. Returns ``{index: Week}`` so a caller (e.g. the sample-log step)
    can look a materialized week back up without re-querying.

    This is also the Phase-3 importer's target hook (plan §5): a parsed Google
    Sheet template maps onto exactly this dict shape.
    """
    slots_by_day = {}
    rows_by_day = {}
    for day_spec in block_spec.get("days", []):
        day_number = day_spec["day_number"]
        slot, _ = SessionSlot.objects.update_or_create(
            mesocycle=mesocycle,
            day_number=day_number,
            defaults={
                "name": day_spec.get("name", ""),
                "bias": day_spec.get("bias", ""),
                "order": day_spec.get("order", day_number - 1),
            },
        )
        slots_by_day[day_number] = slot
        rows = []
        for order, ex in enumerate(day_spec.get("exercises", [])):
            row, _ = ExerciseSlot.objects.update_or_create(
                session_slot=slot,
                order=order,
                defaults={
                    "name": ex["name"],
                    "exercise": ex.get("exercise"),
                    "tags": ex.get("tags", []),
                    "tempo": ex.get("tempo", ""),
                    "rest": ex.get("rest", ""),
                    "note": ex.get("note", ""),
                },
            )
            rows.append(row)
        rows_by_day[day_number] = rows

    weeks_by_index = {}
    for week_spec in block_spec.get("weeks", []):
        week, _ = Week.objects.update_or_create(
            mesocycle=mesocycle,
            index=week_spec["index"],
            defaults={
                "phase": week_spec.get("phase", ""),
                "volume": week_spec.get("volume", 0),
                "intensity": week_spec.get("intensity", 0),
                "is_deload": week_spec.get("is_deload", False),
            },
        )
        weeks_by_index[week_spec["index"]] = week
        # Every live week gets the FULL fixed lineup (invariant: every slot ×
        # live-week has a cell) — a week without explicit ``"cells"`` numbers
        # still materializes the lineup with BLANK cells, not an empty grid, so
        # the block is dense: switching to any week shows the same exercises, and
        # block-wide writes (add day/row) never leave a half-materialized week.
        # Explicit ``"cells"`` numbers apply for the week that specifies them.
        cells_spec = week_spec.get("cells", {})
        for day_number, slot in slots_by_day.items():
            Session.objects.update_or_create(week=week, session_slot=slot)
            row_cells = cells_spec.get(day_number, [])
            for order, row in enumerate(rows_by_day.get(day_number, [])):
                cell = row_cells[order] if order < len(row_cells) else {}
                Prescription.objects.update_or_create(
                    exercise_slot=row,
                    week=week,
                    line=0,
                    defaults={
                        "text": cell.get("text", ""),
                        "skipped": cell.get("skipped", False),
                    },
                )
                for line, line_text in enumerate(cell.get("lines", []), start=1):
                    Prescription.objects.update_or_create(
                        exercise_slot=row,
                        week=week,
                        line=line,
                        defaults={"text": line_text},
                    )
    return weeks_by_index


def _logged_sets_from_cells(log, prescriptions):
    """``LoggedSet`` rows derived from each cell's parsed prescription text.

    Best-effort, mirroring ``parsing.parse_prescription``'s own contract (never
    raises): a cell that doesn't fully parse still logs one set with whatever
    reps/load/rpe *did* parse (blank where it didn't) rather than crashing — a
    %1RM load token (``"72%"``, no absolute bar weight) logs with a blank load.
    """
    rows = []
    for prescription in prescriptions:
        parsed = parse_prescription(prescription.text) or {}
        sets_count = parsed.get("sets") or 1
        reps = parsed.get("reps")
        if reps is None:
            reps_range = parsed.get("reps_range")
            reps_text = f"{reps_range[0]}-{reps_range[1]}" if reps_range else ""
        else:
            reps_text = str(reps)
        load = parsed.get("load") or ""
        if load.endswith("%"):
            load = ""  # a %1RM token isn't a bar weight — leave blank, not crash
        rpe = parsed.get("rpe") or ""
        for set_number in range(1, sets_count + 1):
            rows.append(
                LoggedSet(
                    session_log=log,
                    prescription=prescription,
                    set_number=set_number,
                    reps=reps_text,
                    load=load,
                    rpe=rpe,
                )
            )
    return rows


class Command(BaseCommand):
    help = "Seed the Meso coach-side demo (coach, athletes, relationships, plans)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--coach-email",
            default=DEFAULT_COACH_EMAIL,
            help=f"Email of the demo coach (default: {DEFAULT_COACH_EMAIL}).",
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Tear down the demo (athletes + their links and plans) and exit.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        coach_email = options["coach_email"]

        if options["delete"]:
            self._delete_demo(coach_email)
            return

        today = date.today()
        coach = self._ensure_coach(coach_email)
        for spec in ATHLETES:
            athlete = self._ensure_athlete(spec, today)
            self._ensure_link(coach, athlete)
            plan_spec = PLANS.get(spec["slug"])
            if plan_spec is not None:
                plan = self._ensure_plan(coach, athlete, plan_spec)
                self._log_plan_history(athlete, plan, plan_spec, today)
                if spec["slug"] == "maya":
                    self._ensure_log(athlete, plan, today)
        self._ensure_pending_invite(coach)
        self._ensure_pending_request(coach)
        self._ensure_past_athlete(coach, today)

        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Meso demo seeded for {coach.email}: "
                f"{len(ATHLETES)} athletes, {len(PLANS)} clients with full "
                "programs (every block built + delivered, one logged "
                "multi-week history each), 1 pending invite, "
                "1 pending request, 1 past athlete."
            )
        )

    # -- teardown ---------------------------------------------------------

    def _delete_demo(self, coach_email):
        CoachInvite.objects.filter(
            coach__email=coach_email, email=PENDING_INVITE_EMAIL
        ).delete()
        # Drop the requester (their pending request link cascades with the user).
        User.objects.filter(email=PENDING_REQUEST_EMAIL).delete()
        # Drop the former athlete (their ended link cascades with the user).
        User.objects.filter(email=PAST_ATHLETE_EMAIL).delete()
        emails = [spec["email"] for spec in ATHLETES]
        deleted, _ = User.objects.filter(email__in=emails).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Meso demo torn down ({deleted} rows; demo athletes, links, plans)."
            )
        )

    # -- coach ------------------------------------------------------------

    def _ensure_coach(self, email):
        coach, created = User.objects.get_or_create(
            email=email,
            defaults={"username": email, "name": "Lance Goyke"},
        )
        if created:
            # Fresh dev DB only: a usable, throwaway password printed once so you
            # can log in. An existing coach (the common case) keeps their own.
            password = get_random_string(16)
            coach.set_password(password)
            coach.save(update_fields=["password"])
            self.stdout.write(
                f"  - created coach {email} (temporary password: {password})"
            )
        else:
            self.stdout.write(f"  - using existing coach {email}")

        CoachProfile.objects.update_or_create(
            user=coach,
            defaults={
                "programming_style": COACH_STYLE_TAGS,
                "avoid_rules": COACH_AVOID,
                "default_unit": Unit.KILOGRAMS,
            },
        )
        # The demo coach is the owner — comped, so billing (S6) never paywalls the
        # demo (D12). Idempotent upsert, so a reseed keeps them comped.
        CoachSubscription.comp(coach)
        return coach

    # -- athletes ---------------------------------------------------------

    def _ensure_athlete(self, spec, today):
        athlete, created = User.objects.get_or_create(
            email=spec["email"],
            defaults={
                "username": spec["email"],
                "name": spec["name"],
                "birthday": _years_before(today, spec["age"]),
            },
        )
        if created:
            athlete.set_unusable_password()
            athlete.save(update_fields=["password"])

        AthleteProfile.objects.update_or_create(
            user=athlete,
            defaults={
                "training_started": _months_before(today, spec["trained_months"]),
            },
        )
        for text in spec["contraindications"]:
            Contraindication.objects.get_or_create(
                athlete=athlete, text=text, defaults={"active": True}
            )
        return athlete

    def _ensure_pending_invite(self, coach):
        """A pending email invite (N4) so the roster's onboarding surface shows.

        ``open_for`` reuses the coach's open row on reseed (so duplicates don't
        pile up) and stamps a real TTL (N4 Phase 3), re-arming it if a prior run's
        invite has since aged out.
        """
        CoachInvite.open_for(coach=coach, email=PENDING_INVITE_EMAIL)

    def _ensure_pending_request(self, coach):
        """A pending athlete→coach request (N4 Phase 2) so the surface shows.

        An existing user (with no prior link to the coach) who has asked to
        train under them. ``update_or_create`` on the link keeps a reseed
        idempotent and repairs the row to ``pending_athlete_request`` if a prior
        run (or a manual accept/decline) left it elsewhere.
        """
        requester, created = User.objects.get_or_create(
            email=PENDING_REQUEST_EMAIL,
            defaults={"username": PENDING_REQUEST_EMAIL, "name": PENDING_REQUEST_NAME},
        )
        if created:
            requester.set_unusable_password()
            requester.save(update_fields=["password"])
        CoachAthlete.objects.update_or_create(
            coach=coach,
            athlete=requester,
            defaults={
                "status": CoachAthlete.Status.PENDING_ATHLETE_REQUEST,
                "invited_by": CoachAthlete.InvitedBy.ATHLETE,
                "responded_at": None,
                "ended_at": None,
            },
        )

    def _ensure_past_athlete(self, coach, today):
        """A former athlete on an ENDED link so the history surface shows.

        The relationship-history page ("Past athletes") lists ended/declined
        links — a coach used to train this person, then the relationship ended.
        ``update_or_create`` keeps a reseed idempotent and repairs the row back to
        ``ended`` if a prior run (or a manual reopen) left it elsewhere.
        """
        alum, created = User.objects.get_or_create(
            email=PAST_ATHLETE_EMAIL,
            defaults={
                "username": PAST_ATHLETE_EMAIL,
                "name": PAST_ATHLETE_NAME,
                "birthday": _years_before(today, 31),
            },
        )
        if created:
            alum.set_unusable_password()
            alum.save(update_fields=["password"])
        CoachAthlete.objects.update_or_create(
            coach=coach,
            athlete=alum,
            defaults={
                "status": CoachAthlete.Status.ENDED,
                "invited_by": CoachAthlete.InvitedBy.COACH,
                "responded_at": None,
                "ended_at": timezone.now(),
            },
        )

    def _ensure_link(self, coach, athlete):
        """An active, coach-invited link (the prototype's roster is all-active).

        ``update_or_create`` so a reseed restores the demo link to ``active``
        even if a prior run (or a manual ``end()``) left it pending / declined /
        ended — otherwise the roster and ``Plan.objects.for_coach`` would keep
        excluding the athlete while the command reported success.
        """
        link, _ = CoachAthlete.objects.update_or_create(
            coach=coach,
            athlete=athlete,
            defaults={
                "status": CoachAthlete.Status.ACTIVE,
                "invited_by": CoachAthlete.InvitedBy.COACH,
                "responded_at": None,
                "ended_at": None,
            },
        )
        return link

    # -- a client's sample plan --------------------------------------------

    def _ensure_plan(self, coach, athlete, plan_spec):
        link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
        # ``update_or_create`` restores the demo plan to ``active`` (and the
        # seeded goal/unit) on every run — a stale draft/archived plan would
        # otherwise be skipped by the bare designer/deliver redirect, which only
        # targets non-archived plans.
        plan, _ = Plan.objects.update_or_create(
            relationship=link,
            title=plan_spec["title"],
            defaults={
                "goal": plan_spec["goal"],
                "status": Plan.Status.ACTIVE,
                "unit": Unit.KILOGRAMS,
            },
        )
        # A *complete* hierarchy is left intact — a reseed preserves any coach
        # edits to the demo grid rather than clobbering them. A *partial* one is
        # torn down and rebuilt: a DB seeded by an earlier version of this
        # command has the mesocycle rows but only the Hypertrophy block was ever
        # materialized (the others were planned-length-only), so a bare
        # ``mesocycles.exists()`` check would let that stale shape survive an
        # in-place upgrade. Compare the built shape to the spec instead.
        expected_mesocycles = len(plan_spec["mesocycles"])
        expected_weeks = sum(len(m.get("weeks", [])) for m in plan_spec["mesocycles"])
        built_mesocycles = plan.mesocycles.count()
        built_weeks = Week.objects.filter(
            mesocycle__plan=plan, deleted_at__isnull=True
        ).count()
        if built_mesocycles:
            if (
                built_mesocycles == expected_mesocycles
                and built_weeks == expected_weeks
            ):
                self.stdout.write(
                    f"  - sample plan '{plan.title}' present; ensured active"
                )
                return plan
            # Stale / partial hierarchy — rebuild it from the current spec.
            plan.mesocycles.all().delete()
            self.stdout.write(
                f"  - sample plan '{plan.title}' was partial; rebuilding hierarchy"
            )

        for meso_spec in plan_spec["mesocycles"]:
            mesocycle = Mesocycle.objects.create(
                plan=plan,
                name=meso_spec["name"],
                order=meso_spec["order"],
                week_count=meso_spec["week_count"],
            )
            build_block(mesocycle, meso_spec)
        self.stdout.write(f"  - built sample plan '{plan.title}' for {athlete.name}")
        return plan

    # -- delivery + logged history ------------------------------------------

    def _log_plan_history(self, athlete, plan, plan_spec, today):
        """Deliver every live week of ``plan``; log the ones before its cutoff.

        2d: delivery no longer gates what the athlete sees, so there's no
        "future, undelivered" distinction left to model — every live week
        simply gets ``delivered_at`` stamped, unconditionally (docs/meso/
        remove-current-week-plan.md §6).

        A realistic multi-week logged training history is still worth
        demoing, so ``plan_spec``'s ONE mesocycle dict carrying a
        ``logged_through_index`` (a plain seed-data marker set by ``_block``/
        ``_client_block`` — never a materialized ``Week`` field, since that
        field no longer exists) marks how deep to log: every week strictly
        before that ``(mesocycle order, index)`` point gets every one of its
        (non-skipped) prescriptions logged as a completed session, dated
        further into the past the earlier it falls in the program. A plan
        with no such marker (no mesocycle sets it) is delivered only, never
        logged. The cutoff week itself is never logged here: Maya's
        hand-authored cutoff-week Day-1 log is layered on separately by
        ``_ensure_log``; Devon/Priya's cutoff week is left unlogged, matching
        the demo's original design.

        Idempotent: a week's ``SessionLog``s are (re)created only when absent
        (mirrors ``_ensure_log``'s create-if-absent contract), so a reseed
        never duplicates the history.
        """
        live_weeks = list(
            Week.objects.filter(mesocycle__plan=plan, deleted_at__isnull=True)
            .select_related("mesocycle")
            .order_by("mesocycle__order", "index")
        )
        now = timezone.now()
        for week in live_weeks:
            if week.delivered_at is None:
                week.delivered_at = now
                week.save(update_fields=["delivered_at"])

        cutoff = None
        cutoff_meso_spec = None
        for meso_spec in plan_spec["mesocycles"]:
            idx = meso_spec.get("logged_through_index")
            if idx is not None:
                cutoff = (meso_spec["order"], idx)
                cutoff_meso_spec = meso_spec
                break
        if cutoff is None:
            return

        total = len(live_weeks)
        logged_prescriptions = []
        logged_weeks = 0
        for position, week in enumerate(live_weeks):
            if (week.mesocycle.order, week.index) >= cutoff:
                continue
            weeks_ago = total - position
            logged_weeks += 1
            for session in week.sessions.filter(deleted_at__isnull=True).select_related(
                "session_slot"
            ):
                log, created = SessionLog.objects.get_or_create(
                    session=session,
                    athlete=athlete,
                    defaults={
                        "status": SessionLog.Status.DONE,
                        "date": today
                        - timedelta(weeks=weeks_ago)
                        + timedelta(days=(session.day_number - 1) * 2),
                    },
                )
                prescriptions = list(session.trainable_cells())
                logged_prescriptions.extend(prescriptions)
                if not created and log.sets.exists():
                    continue
                log.sets.all().delete()
                LoggedSet.objects.bulk_create(
                    _logged_sets_from_cells(log, prescriptions)
                )

        if logged_prescriptions:
            refresh_one_rms(athlete, logged_prescriptions, plan.unit)
        if logged_weeks:
            self.stdout.write(
                f"  - logged {logged_weeks} weeks of history for {athlete.name} "
                f"(through {cutoff_meso_spec['name']} wk{cutoff[1]})"
            )

    # -- the sample logged session ----------------------------------------

    def _ensure_log(self, athlete, plan, today):
        """Deliver + log Maya's logged-through-week "Lower" session (the first real log).

        Idempotent: the week is delivered once (the coach workflow's step
        order, not a gate — 2d), and the ``SessionLog`` + ``LoggedSet`` rows are
        created only if absent, so a reseed never duplicates or clobbers a hand-
        edited log. Returns None if the plan's hierarchy isn't present.
        """
        session = (
            Session.objects.filter(
                week__mesocycle__plan=plan,
                week__mesocycle__name=SAMPLE_LOG["mesocycle"],
                week__index=SAMPLE_LOG["week_index"],
                session_slot__day_number=SAMPLE_LOG["day_number"],
            )
            .select_related("week", "session_slot")
            .first()
        )
        if session is None:
            return None

        week = session.week
        if week.delivered_at is None:
            week.delivered_at = timezone.now()
            week.save(update_fields=["delivered_at"])

        log, created = SessionLog.objects.get_or_create(
            session=session,
            athlete=athlete,
            defaults={
                "status": SessionLog.Status.DONE,
                "date": today - timedelta(days=SAMPLE_LOG["logged_days_ago"]),
            },
        )
        # ``session.cells()`` = this week's live Prescription cells for this
        # day's ExerciseSlot rows (replaces the old ``session.prescriptions``).
        prescriptions = {p.name: p for p in session.cells()}
        if not created and log.sets.exists():
            self.stdout.write("  - sample logged session present; left intact")
        else:
            log.sets.all().delete()
            rows = []
            for name, sets in SAMPLE_LOG["sets"].items():
                prescription = prescriptions.get(name)
                if prescription is None:
                    continue
                for set_number, (reps, load, rpe) in enumerate(sets, start=1):
                    rows.append(
                        LoggedSet(
                            session_log=log,
                            prescription=prescription,
                            set_number=set_number,
                            reps=reps,
                            load=load,
                            rpe=rpe,
                        )
                    )
            LoggedSet.objects.bulk_create(rows)
            self.stdout.write(
                f"  - logged sample session '{session.name}' for {athlete.name}"
            )

        # Derive Maya's estimated 1RM from the logged session (the seed writes the
        # log directly, so the log endpoint's refresh hasn't run) — so the demo's
        # %1RM Box Squat shows a real 1RM in the designer + her logger. Idempotent.
        refresh_one_rms(athlete, list(prescriptions.values()), plan.unit)
        return log
