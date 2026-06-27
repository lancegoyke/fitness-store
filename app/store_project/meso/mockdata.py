"""Canonical mock data for the Meso program-designer prototype.

Every Meso screen reads from this one module so the prototype stays internally
consistent — the same athletes, program, and proposed changes appear across the
roster, profile, review, deliver, and results screens. None of this is real:
there is no database, no athlete records, no logged sessions. When the prototype
graduates, these structures map onto real models (users / exercises / a future
programs app) and this module goes away.
"""

# --- athletes -------------------------------------------------------------

ATHLETES = [
    {
        "slug": "maya",
        "name": "Maya Okonkwo",
        "initials": "MO",
        "age": 34,
        "level": "Intermediate",
        "trained": "14 mo trained",
        "goals": ["Hypertrophy", "Return to lifting"],
        "contraindications": [
            "L knee — avoid deep knee flexion under load",
            "No max-effort jumping / impact",
        ],
        "block": "Hypertrophy",
        "week": "Wk 2 / 4",
        "status": "needs_review",
        "status_label": "Needs review",
        "compliance": 92,
        "flags": ["L knee"],
        "tone": "accent",
    },
    {
        "slug": "devon",
        "name": "Devon Reyes",
        "initials": "DR",
        "age": 28,
        "level": "Beginner",
        "trained": "6 mo trained",
        "goals": ["General fitness", "Strength"],
        "contraindications": ["R shoulder — neutral-grip pressing only"],
        "block": "Hypertrophy",
        "week": "Wk 2 / 4",
        "status": "delivered",
        "status_label": "Delivered",
        "compliance": 78,
        "flags": ["R shoulder"],
        "tone": "neutral",
    },
    {
        "slug": "priya",
        "name": "Priya Nair",
        "initials": "PN",
        "age": 41,
        "level": "Advanced",
        "trained": "6 yr trained",
        "goals": ["Powerlifting peak"],
        "contraindications": [],
        "block": "Strength",
        "week": "Wk 1 / 4",
        "status": "active",
        "status_label": "On track",
        "compliance": 96,
        "flags": [],
        "tone": "neutral",
    },
    {
        "slug": "marcus",
        "name": "Marcus Tan",
        "initials": "MT",
        "age": 35,
        "level": "Intermediate",
        "trained": "3 yr trained",
        "goals": ["Hypertrophy"],
        "contraindications": [],
        "block": "Hypertrophy",
        "week": "Wk 2 / 4",
        "status": "drafting",
        "status_label": "Agent drafting",
        "compliance": 84,
        "flags": [],
        "tone": "neutral",
    },
    {
        "slug": "lena",
        "name": "Lena Kovic",
        "initials": "LK",
        "age": 31,
        "level": "Intermediate",
        "trained": "2 yr trained",
        "goals": ["General fitness"],
        "contraindications": ["Lower back — trap-bar / RDL only, no conventional pull"],
        "block": "Hypertrophy",
        "week": "Wk 2 / 4",
        "status": "delivered",
        "status_label": "Delivered",
        "compliance": 88,
        "flags": ["Lower back"],
        "tone": "neutral",
    },
]

GROUPS = [
    {
        "slug": "hypertrophy-group",
        "name": "Hypertrophy Group",
        "focus": "General fitness",
        "members": ["maya", "devon", "priya", "marcus", "lena"],
        "week": "Wk 2 / 4",
        "status": "needs_review",
        "status_label": "Needs review",
        "flag_note": "3 of 5 carry a knee or shoulder flag — auto-regressions applied per athlete",
    },
]

# --- programming context (shared with the designer) -----------------------

COACH_STYLE = {
    "tags": [
        "Compound-first",
        "RPE-based load",
        "Free-weight bias",
        "2-min rest cap",
        "Unilateral work",
    ],
    "avoid": "machine-only days, untracked progressions, >3 exercises to failure / session.",
}

MACROCYCLE = [
    {"name": "Base / GPP", "weeks": "4 wk", "state": "done", "note": "done"},
    {"name": "Hypertrophy", "weeks": "4 wk", "state": "current", "note": "wk 2 / 4"},
    {"name": "Strength", "weeks": "4 wk", "state": "next", "note": "next"},
    {"name": "Peak / Test", "weeks": "2 wk", "state": "future", "note": ""},
]

# --- agent change review (proposed batch awaiting coach approval) ----------

PROPOSED_CHANGES = [
    {
        "id": "swap-knee",
        "kind": "Swap",
        "day": "Day 1 · Lower",
        "title": "Bulgarian Split Squat → Box Step-Down (low)",
        "before": "Bulgarian Split Squat (DB) · 3×10 @ 18 kg",
        "after": "Box Step-Down (low) · 3×10 @ 14 kg",
        "rationale": (
            "Same single-leg quad stimulus, but the knee tracks through a shorter, "
            "controlled range — a better fit for the meniscus history."
        ),
        "honors": "L knee — avoid deep knee flexion under load",
    },
    {
        "id": "progress",
        "kind": "Progress",
        "day": "Day 3 · Posterior",
        "title": "Trap-Bar Deadlift → 92.5 kg",
        "before": "4×6 @ 90 kg · logged RPE 6 last block",
        "after": "4×6 @ 92.5 kg · projected RPE 7",
        "rationale": (
            "Anchored to last block's logged load and RPE — +2.5 kg lands in the "
            "hypertrophy window without overshooting."
        ),
        "honors": "RPE-based load",
    },
    {
        "id": "volume",
        "kind": "Volume",
        "day": "Day 2 · Upper",
        "title": "Day 2 pressing volume − 1 set",
        "before": "Incline DB Press, Chest-Supported Row, Lat Pulldown · 4 sets each",
        "after": "Primary lifts · 3 sets each (accessories unchanged)",
        "rationale": (
            "Keeps weekly pressing volume in check while the shoulder settles, without "
            "touching accessory work."
        ),
        "honors": "R shoulder — neutral-grip pressing only",
    },
]

# --- session results (Maya, delivered Week 1 Day 1) -----------------------

RESULTS_SUMMARY = {
    "athlete": "maya",
    "session": "Week 1 · Day 1 — Lower",
    "logged": "Wed, 6:42 pm",
    "completion": 94,
    "avg_rpe_delta": "+0.4",
    "flag": "Box Squat ran 1.5 RPE over target on the top set — agent suggests holding load next session.",
}

RESULTS_ROWS = [
    {
        "name": "Box Squat (to parallel)",
        "target": "4×6 @ 70 kg · RPE 7",
        "logged": "4×6 @ 70 kg",
        "rpe": "8.5",
        "rpe_state": "over",
        "note": "Top set felt heavy — bar speed dropped",
    },
    {
        "name": "Bulgarian Split Squat (DB)",
        "target": "3×10 @ 18 kg · RPE 7",
        "logged": "3×10 @ 18 kg",
        "rpe": "7",
        "rpe_state": "on",
        "note": "",
    },
    {
        "name": "Leg Press (controlled ROM)",
        "target": "3×12 @ 110 kg · RPE 8",
        "logged": "3×12 @ 110 kg",
        "rpe": "8",
        "rpe_state": "on",
        "note": "",
    },
    {
        "name": "Seated Leg Curl",
        "target": "3×12 @ 41 kg · RPE 8",
        "logged": "2×12, 1×9 @ 41 kg",
        "rpe": "9",
        "rpe_state": "over",
        "note": "Missed 3 reps on last set",
    },
    {
        "name": "Standing Calf Raise",
        "target": "4×15 @ 60 kg",
        "logged": "4×15 @ 60 kg",
        "rpe": "—",
        "rpe_state": "on",
        "note": "",
    },
]

# --- roster activity feed -------------------------------------------------

ACTIVITY = [
    {
        "who": "maya",
        "text": "logged Week 1 · Day 1 — 94% complete",
        "when": "2h ago",
        "kind": "log",
    },
    {
        "who": "devon",
        "text": "flagged shoulder discomfort on Incline DB Press",
        "when": "5h ago",
        "kind": "flag",
    },
    {
        "who": "priya",
        "text": "hit a 3-rep PR on back squat — 142.5 kg",
        "when": "Yesterday",
        "kind": "pr",
    },
    {
        "who": "marcus",
        "text": "agent finished drafting Week 2 — awaiting your review",
        "when": "Yesterday",
        "kind": "draft",
    },
    {
        "who": "lena",
        "text": "completed Week 1 — 88% adherence",
        "when": "2d ago",
        "kind": "log",
    },
]

# --- deliver payload ------------------------------------------------------

DELIVER = {
    "athlete": "maya",
    "what": "Week 2 — Accumulation",
    "sessions": 3,
    "starts": "Wed, Jun 24",
    "changes_since": [
        "Box Step-Down swapped in for Bulgarian Split Squat (knee)",
        "Trap-Bar Deadlift progressed to 92.5 kg",
        "Day 2 pressing trimmed by 1 set",
    ],
    "channels": ["Push notification", "Email summary"],
}


def athlete_by_slug(slug):
    for a in ATHLETES:
        if a["slug"] == slug:
            return a
    return None


def athletes_map():
    return {a["slug"]: a for a in ATHLETES}
