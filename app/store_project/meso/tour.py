"""Guided demo onboarding tour — Phase 2 (issue #430).

Phase 1 (``docs/meso/demo-onboarding-tour-plan.md``) split ``demo.load_demo``
into idempotent, per-feature segment loaders. This phase drives them one at a
time from an in-app guided tour: eight steps, each introducing a feature and
(where relevant) offering an "add sample data" action tied to one segment.

``STEPS`` is the single source of truth — the front-end config
(``build_config``), the tour-state endpoint, and the tests all read from it.
Progress persists server-side on ``CoachProfile.tour_state`` (O2): only the
step index + dismissed/complete flag are stored — whether a step's *data* is
loaded is always derived (``has_athletes``/``has_program``/... via
``_segment_loaded``), never stored (O7), mirroring ``demo.has_demo``.

Phase 2 is sandbox-only (``is_active``/``build_config`` don't gate on
audience themselves — the caller, ``context_processors.sandbox_status``,
combines this module's state check with ``sandbox.is_sandbox`` so a real
coach's page never even calls into here). This module intentionally has no
import of ``sandbox`` — ``sandbox.create_sandbox`` imports *this* module to
seed the initial tour state, and keeping the dependency one-directional avoids
a circular import.
"""

from django.urls import reverse

from . import demo as meso_demo
from .models import CoachProfile

#: The 8-step sandbox tour (O2/O3). ``segment`` names a ``meso_demo.SEGMENTS``
#: loader (or ``None`` when the step has no data action); ``anchor`` is the
#: ``data-tour`` value spotlighted on the step's page (``None`` → centered
#: card, no spotlight). ``url_name`` is reversed server-side into each step's
#: "Take me there" link — every one of these is a bare, no-argument URL that
#: already redirects to "the coach's current thing" (working plan / latest
#: session / roster), so the tour never needs to know *which* athlete/plan.
STEPS = [
    {
        "key": "welcome",
        "title": "Where your clients live",
        "body": "This is your roster — every athlete you coach, at a glance. "
        "Add 5 sample athletes to see it in action.",
        "url_name": "meso:roster",
        "anchor": "roster-individuals",
        "segment": "athletes",
        "action_label": "Add 5 sample athletes",
    },
    {
        "key": "profile",
        "title": "One athlete, one record",
        "body": "Click into Maya to see her contraindications, training "
        "history, and program — everything you need before you build for her.",
        "url_name": "meso:roster",
        "anchor": "roster-athlete-rows",
        "segment": None,
        "action_label": None,
    },
    {
        "key": "designer",
        "title": "Program Designer",
        "body": "The flagship: lay out a mesocycle week by week. Load a "
        "sample program to see a full block.",
        "url_name": "meso:designer",
        "anchor": "designer-root",
        "segment": "program",
        "action_label": "Load a sample mesocycle",
    },
    {
        "key": "deliver",
        "title": "Deliver to their phone",
        "body": "Push the current week straight to the athlete's phone — "
        "she sees it the moment you send it.",
        "url_name": "meso:deliver",
        "anchor": "deliver-send",
        "segment": "delivery",
        "action_label": "Deliver the sample week",
    },
    {
        "key": "results",
        "title": "What actually happened",
        "body": "Logged sets, adherence, and estimated 1RM flow back here "
        "the moment she logs a session.",
        "url_name": "meso:results",
        "anchor": "results-main",
        "segment": "log",
        "action_label": "Log a sample session",
    },
    {
        "key": "groups",
        "title": "Program a whole group at once",
        "body": "Groups share one program with per-athlete auto-adjusts — "
        "build it once, tune it per person.",
        "url_name": "meso:roster",
        "anchor": "roster-groups",
        "segment": "group",
        "action_label": "Add a sample group",
    },
    {
        "key": "agent",
        "title": "Adapt · the AI agent",
        "body": "The agent reads an athlete's fatigue and adherence and "
        "proposes next week's adjustments. Create a free account to run it "
        "for real.",
        "url_name": "meso:designer",
        "anchor": "designer-root",
        "segment": None,
        "action_label": None,
        # Sandbox-only step: no data action, just an explanation + the signup
        # gate the agent endpoint already enforces (guards in test_sandbox.py).
        "signup_gate": True,
    },
    {
        "key": "finish",
        "title": "You're ready",
        "body": "Create a free account to keep coaching for real — or "
        "remove this demo data and start over.",
        "url_name": "meso:roster",
        "anchor": None,
        "segment": None,
        "action_label": None,
        "signup_gate": True,
    },
]

#: A ``tour_state.status`` that hides the tour entirely.
_HIDDEN_STATUSES = {"dismissed", "completed"}

#: segment name → the ``demo.has_*`` predicate that derives its loaded-ness
#: (O7 — never stored, always read off the data).
_HAS_PREDICATES = {
    "athletes": meso_demo.has_athletes,
    "program": meso_demo.has_program,
    "delivery": meso_demo.has_delivery,
    "log": meso_demo.has_log,
    "group": meso_demo.has_group,
}


def _clamp(step):
    """Coerce ``step`` into a valid index, clamped into ``STEPS``' range."""
    try:
        step = int(step)
    except (TypeError, ValueError):
        step = 0
    return max(0, min(step, len(STEPS) - 1))


def tour_status(user):
    """``user``'s raw ``tour_state`` dict, or ``{}`` if never started / no profile."""
    profile = CoachProfile.objects.filter(user=user).only("tour_state").first()
    if profile is None:
        return {}
    return profile.tour_state or {}


def is_active(user):
    """Whether the tour should still render for ``user`` (not dismissed/completed).

    Doesn't check audience (sandbox vs. real coach) itself — Phase 2 callers
    (``context_processors.sandbox_status``) combine this with
    ``sandbox.is_sandbox`` before ever rendering anything.
    """
    return tour_status(user).get("status") not in _HIDDEN_STATUSES


def start_tour(profile):
    """Reset ``profile`` to step 0, active — the sandbox's initial state and ``restart``."""
    profile.tour_state = {"step": 0, "status": "active"}
    profile.save(update_fields=["tour_state"])
    return profile.tour_state


def set_step(profile, step):
    """Move ``profile`` to ``step`` (clamped into range), staying active."""
    profile.tour_state = {"step": _clamp(step), "status": "active"}
    profile.save(update_fields=["tour_state"])
    return profile.tour_state


def dismiss(profile):
    """Hide the tour without finishing it — the step index is preserved."""
    step = _clamp((profile.tour_state or {}).get("step", 0))
    profile.tour_state = {"step": step, "status": "dismissed"}
    profile.save(update_fields=["tour_state"])
    return profile.tour_state


def complete(profile):
    """Mark the tour finished — parked on the last step, ``completed``."""
    profile.tour_state = {"step": len(STEPS) - 1, "status": "completed"}
    profile.save(update_fields=["tour_state"])
    return profile.tour_state


def _segment_loaded(user, segment):
    """Whether ``segment``'s data already exists for ``user`` (O7), or ``None``."""
    predicate = _HAS_PREDICATES.get(segment)
    return bool(predicate(user)) if predicate else None


def build_config(user):
    """The front-end tour config.

    Every step (with a resolved URL + loaded state) plus the coach's current
    progress and the endpoints the driver posts to. Returns ``None`` if
    ``user`` has no ``CoachProfile`` to read progress from (defensive —
    callers gate on ``is_active``/``is_sandbox`` first, and every sandbox
    coach has one).
    """
    profile = CoachProfile.objects.filter(user=user).first()
    if profile is None:
        return None
    state = profile.tour_state or {}
    steps = [
        {
            "key": step["key"],
            "title": step["title"],
            "body": step["body"],
            "url": reverse(step["url_name"]),
            "anchor": step["anchor"],
            "segment": step["segment"],
            "action_label": step["action_label"],
            "signup_gate": step.get("signup_gate", False),
            "loaded": _segment_loaded(user, step["segment"]),
        }
        for step in STEPS
    ]
    return {
        "steps": steps,
        "step": _clamp(state.get("step", 0)),
        "status": state.get("status", "active"),
        "state_url": reverse("meso:tour_state"),
        "skip_url": reverse("meso:tour_skip"),
        "demo_load_url": reverse("meso:demo_load"),
        "signup_url": reverse("meso:sandbox_signup"),
    }
