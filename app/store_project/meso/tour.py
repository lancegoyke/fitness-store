"""Guided demo onboarding tour — Phase 2 (sandbox) + Phase 3 (real coach), issue #430.

Phase 1 (``docs/meso/demo-onboarding-tour-plan.md``) split ``demo.load_demo``
into idempotent, per-feature segment loaders. Phase 2 drove them one at a time
from an in-app guided tour for the anonymous sandbox. Phase 3 extends the same
eight steps to a real, authenticated coach with an empty workspace: instead of
loading fake demo athletes, they're guided to add *themselves* as an athlete
and program for themselves (O5) — the self-coaching variant.

``STEPS`` is still the single source of truth, but each step now carries two
sub-dicts, ``"sandbox"`` and ``"self"``, for the copy/action that vary by
audience; ``key``/``url_name``/``anchor`` stay shared/top-level (a step's
*page* is the same regardless of who's touring it — anchor overrides only
where the spotlighted control itself differs, e.g. ``finish``'s
"roster-invite"). ``build_config(user, variant)`` resolves whichever variant
into the exact flat step shape the JS driver already consumed in Phase 2, plus
one additive field (``action``) the self variant uses for its typed
actions — sandbox steps still produce ``segment``/``action_label``/
``signup_gate`` exactly as before, so the sandbox contract (front-end and
tests) is untouched.

The **variant is derived, never stored** (O7 spirit, decision 2): a throwaway
sandbox coach always gets ``"sandbox"``; every other authenticated coach gets
``"self"``. ``tour_state`` keeps only the step index + dismissed/complete
flag, exactly as in Phase 2 — nothing here persists *which* variant a coach
is on.

This module still has no *top-level* import of ``sandbox`` — ``sandbox.
create_sandbox`` imports *this* module at the top level to seed the initial
tour state, and a top-level import back would form a real cycle. ``variant_
for`` breaks that with a deferred (function-body) import instead, which is
safe because by the time any request calls it, both modules are fully loaded.

Phase 4 (analytics + polish) adds the funnel-event recording at the bottom of
this module (``record_started``/``record_advanced``/``record_dismissed``/
``record_completed``/``record_skipped``/``record_opt_in``) — see ``TourEvent``
in ``models.py`` for why a meso-local table rather than the ``analytics`` app.
"""

import logging

from django.db import transaction
from django.urls import reverse

from . import demo as meso_demo
from .billing import access as billing_access
from .models import CoachAthlete
from .models import CoachProfile
from .models import MesoGroup
from .models import SessionLog
from .models import TourEvent
from .models import Week

logger = logging.getLogger(__name__)

#: The 8-step tour (O2/O3), shared across audiences. ``url_name``/``anchor``
#: are the step's "Take me there" target + spotlighted control — the same
#: physical page/control regardless of variant (``finish`` is the one
#: exception: the self variant overrides ``anchor`` in its own sub-dict,
#: since a real coach spotlights the roster's invite control there instead of
#: nothing). ``"sandbox"``/``"self"`` carry the varying title/body/action.
STEPS = [
    {
        "key": "welcome",
        "url_name": "meso:roster",
        "anchor": "roster-individuals",
        "sandbox": {
            "title": "Where your clients live",
            "body": "This is your roster — every athlete you coach, at a glance. "
            "Add 5 sample athletes to see it in action.",
            "body_done": "This is your roster — every athlete you coach, at a "
            "glance. Your five sample athletes are here now.",
            "segment": "athletes",
            "action_label": "Add 5 sample athletes",
        },
        "self": {
            "title": "Where your clients live",
            "body": "This is your roster — every athlete you coach, at a glance. "
            "Add yourself as your first athlete to see it in action.",
            "body_done": "This is your roster — every athlete you coach, at a "
            "glance. You're on it now as your first athlete.",
            "action_label": "Add yourself as your first athlete",
        },
    },
    {
        "key": "profile",
        "url_name": "meso:roster",
        # Sandbox spotlights the whole list: the athlete the copy names (Maya)
        # can't be picked out row-by-row here — the roster is sorted by name
        # (Devon sorts first) and her demo program only loads at a later step,
        # so there's no row-level signal for "Maya" yet (#441 P1-2). The self
        # variant has exactly one row (the coach's own) and re-anchors to it
        # for a precise spotlight.
        "anchor": "roster-athlete-rows",
        "sandbox": {
            "title": "One athlete, one record",
            "body": "Click into Maya to see her contraindications, training "
            "history, and program — everything you need before you build for her.",
            # Gated: there's no athlete to click into until the welcome step
            # loads them, so the copy asks for that first (#441 P2-2).
            "requires_segment": "athletes",
            "body_locked": "Add the sample athletes first (the welcome step), "
            "then click into one to see contraindications, training history, "
            "and program.",
        },
        "self": {
            "title": "One athlete, one record",
            "body": "Click into your own profile to see the same contraindications, "
            "training history, and program view every athlete gets.",
            "body_locked": "Add yourself as an athlete first (the welcome step), "
            "then open your profile to see the contraindications, training "
            "history, and program view every athlete gets.",
            "anchor": "roster-athlete-row-first",
        },
    },
    {
        "key": "designer",
        "url_name": "meso:designer",
        "anchor": "designer-root",
        "sandbox": {
            "title": "Program Designer",
            "body": "The flagship: lay out a mesocycle week by week. Load a "
            "sample program to see a full block.",
            "body_done": "The flagship: lay out a mesocycle week by week. The "
            "sample program is loaded — here's a full block, week by week.",
            "segment": "program",
            "action_label": "Load a sample mesocycle",
        },
        "self": {
            "title": "Program Designer",
            "body": "The flagship: lay out a mesocycle week by week. Start a "
            "program for yourself to see a full block take shape.",
            "body_done": "The flagship: lay out a mesocycle week by week. Your "
            "program is started — here's a full block taking shape.",
            "action_label": "Start a program for yourself",
            # Shown instead of ``body`` when there's no self-link yet (the
            # action itself is omitted too — step 1 has to come first).
            "locked_body": "Add yourself as an athlete first (the welcome "
            "step) — then come back here to build your own program.",
        },
    },
    {
        "key": "deliver",
        "url_name": "meso:deliver",
        "anchor": "deliver-send",
        "sandbox": {
            "title": "Deliver to their phone",
            "body": "Push the current week straight to the athlete's phone — "
            "she sees it the moment you send it.",
            "body_done": "Delivered — the athlete sees this week on her phone "
            "now, exactly as you sent it.",
            "segment": "delivery",
            "action_label": "Deliver the sample week",
        },
        "self": {
            "title": "Deliver to your phone",
            "body": "Push the current week straight to your own phone — "
            "you'll see it the moment you send it, exactly like any athlete would.",
            "body_done": "Delivered — this week is on your phone now, exactly as "
            "you sent it.",
        },
    },
    {
        "key": "results",
        "url_name": "meso:results",
        "anchor": "results-main",
        "sandbox": {
            "title": "What actually happened",
            "body": "Logged sets, adherence, and estimated 1RM flow back here "
            "the moment she logs a session.",
            "body_done": "A sample session is logged — here are the sets, "
            "adherence, and estimated 1RM flowing back.",
            "segment": "log",
            "action_label": "Log a sample session",
        },
        "self": {
            "title": "What actually happened",
            "body": "Log your own sets from your phone at /meso/me/ — logged "
            "sets, adherence, and estimated 1RM flow back here the moment you do.",
            "body_done": "Your session is logged — here are the sets, adherence, "
            "and estimated 1RM flowing back.",
        },
    },
    {
        "key": "groups",
        "url_name": "meso:roster",
        "anchor": "roster-groups",
        "sandbox": {
            "title": "Program a whole group at once",
            "body": "Groups share one program with per-athlete auto-adjusts — "
            "build it once, tune it per person.",
            "body_done": "Sample group added — one shared program with "
            "per-athlete auto-adjusts, built once and tuned per person.",
            "segment": "group",
            "action_label": "Add a sample group",
        },
        "self": {
            "title": "Program a whole group at once",
            "body": "Groups share one program with per-athlete auto-adjusts — "
            "built for when you're coaching several athletes together. Coaching "
            "just yourself for now? Skip this one.",
            "body_done": "Your group exists — one shared program with per-athlete "
            "auto-adjusts, built once and tuned per person.",
        },
    },
    {
        "key": "agent",
        "url_name": "meso:designer",
        "anchor": "designer-root",
        "sandbox": {
            "title": "Adapt · the AI agent",
            "body": "The agent reads an athlete's fatigue and adherence and "
            "proposes next week's adjustments. Create a free account to run it "
            "for real.",
            # Sandbox-only: no data action, just an explanation + the signup
            # gate the agent endpoint already enforces (test_sandbox.py).
            "signup_gate": True,
        },
        "self": {
            "title": "Adapt · the AI agent",
            "body": "The agent reads your fatigue and adherence and drafts "
            "next week's adjustments. Draft your next block with it now.",
            "action_label": "Draft next block with AI",
            # Shown when the action isn't offered — one of three distinct
            # blockers (#441 P1-5: the old single generic explanation didn't
            # name the actual reason, so it could tell a coach who already had
            # a plan to go get one).
            "locked_body_no_link": "Add yourself as an athlete first (the "
            "welcome step) — then the agent can draft a training block for you.",
            "locked_body_has_plan": "Your block is already drafted — open it in "
            "the Program Designer, where the agent can adapt it from here.",
            "locked_body_no_allowance": "You're out of free AI drafts for now — "
            "start a trial to keep drafting next blocks with the agent.",
        },
    },
    {
        "key": "finish",
        "url_name": "meso:roster",
        "anchor": None,
        "sandbox": {
            "title": "You're ready",
            # Default (has-demo) copy: only promise removal when there's demo
            # data to remove, and never imply a tour "start over" — the real
            # restart path is the durable sidebar affordance (#441 P2-4/P2-3).
            "body": "You've seen the whole workflow. Create a free account to "
            "keep coaching for real — your sample data stays private and you "
            "can remove it any time.",
            "body_no_demo": "You've seen the whole workflow. Create a free "
            "account to keep coaching for real.",
            "signup_gate": True,
        },
        "self": {
            "title": "You're ready",
            "body": "Invite your first real athlete — your roster is yours from here.",
            # Spotlights the roster's real "+ Invite an athlete" control
            # (``data-tour="roster-invite"``) instead of the centered,
            # anchor-less card the sandbox's signup-gated finish step uses.
            "anchor": "roster-invite",
        },
    },
]

#: A ``tour_state.status`` that hides the tour entirely.
_HIDDEN_STATUSES = {"dismissed", "completed"}

#: segment name → the ``demo.has_*`` predicate that derives its loaded-ness
#: (O7 — never stored, always read off the data). Sandbox variant only.
_HAS_PREDICATES = {
    "athletes": meso_demo.has_athletes,
    "program": meso_demo.has_program,
    "delivery": meso_demo.has_delivery,
    "log": meso_demo.has_log,
    "group": meso_demo.has_group,
}

#: sandbox segment name → the step ``key`` that offers it (Phase 4 funnel
#: events: ``demo_load``'s segment POST field doesn't otherwise carry which
#: step fired it). Built from ``STEPS`` so it can't drift out of sync.
_STEP_KEY_BY_SEGMENT = {
    step["sandbox"]["segment"]: step["key"]
    for step in STEPS
    if step["sandbox"].get("segment")
}


def step_key_for_segment(segment):
    """The tour step ``key`` that offers ``segment``, or ``""`` if unknown."""
    return _STEP_KEY_BY_SEGMENT.get(segment, "")


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

    Doesn't check audience (sandbox vs. real coach) itself, and doesn't
    distinguish "never started" from "in progress" — both read as active
    here. That's exactly right for the sandbox (whose tour is armed at step 0
    the instant ``create_sandbox`` runs, so "never started" can't happen) and
    for the roster's tour-entry-card decision (a real coach who never started
    OR is mid-tour should never see the *original* Get-started card). It is
    **not** the real-coach *mount* gate, though — a real coach's never-started
    ``{}`` must not auto-mount the tour (O2); see ``is_touring`` for that
    stricter check.
    """
    return tour_status(user).get("status") not in _HIDDEN_STATUSES


def is_touring(user):
    """Whether a real coach's tour is *explicitly* active right now (Phase 3 gate).

    Unlike ``is_active`` (which also treats a never-started ``{}`` state as
    active — fine for the sandbox, which is always armed from creation), a
    real coach only sees the tour mounted once they've explicitly opted in —
    the roster's "Start the guided tour" button, which fires ``tour_state``'s
    ``restart`` action and always writes a literal ``status: "active"``. A
    coach who has never touched the tour reads ``{}`` here (status ``None``,
    not the string ``"active"``), so this is False and the tour never
    self-mounts on them.
    """
    return tour_status(user).get("status") == "active"


def variant_for(user):
    """Which step variant applies to ``user`` — sandbox coach vs. real coach (O5).

    Derived, never stored (decision 2): a throwaway sandbox coach gets the
    fake-data ``"sandbox"`` steps; every other authenticated coach gets the
    self-coaching ``"self"`` steps. The ``sandbox`` import is deferred to the
    function body — ``sandbox.create_sandbox`` imports this module at the top
    level, so importing it back at *this* module's top level would form a
    real cycle; by the time any caller actually invokes this function both
    modules are fully loaded, so the deferred import always succeeds.
    """
    from . import sandbox as meso_sandbox

    return "sandbox" if meso_sandbox.is_sandbox(user) else "self"


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


def advance_if_on_step(user, step_key):
    """Auto-advance a touring coach off ``step_key`` on a page visit (#441 P1-2).

    The button-less ``profile`` step has no data action to flip a
    ``loaded`` flag — it completes when the coach actually opens an athlete
    profile, so the server advances the tour when that visit happens
    (``AthleteProfileView``). A strict no-op unless the coach is *actively*
    touring (status ``active`` — not dismissed/completed/never-started)
    **and** parked exactly on ``step_key``: revisiting the page later, or
    visiting while parked elsewhere, does nothing. Advances one step and
    records the ``advanced`` funnel event (mirroring ``tour_state``'s
    goto/advance path). Returns whether it advanced.
    """
    profile = CoachProfile.objects.filter(user=user).first()
    if profile is None:
        return False
    state = profile.tour_state or {}
    if state.get("status") != "active":
        return False
    current = _clamp(state.get("step", 0))
    if STEPS[current]["key"] != step_key or current >= len(STEPS) - 1:
        return False
    set_step(profile, current + 1)
    record_advanced(user, variant_for(user), STEPS[current + 1]["key"])
    return True


def current_step_key(user):
    """The key of the step a coach is *actively* parked on, else ``None``.

    Mirrors ``advance_if_on_step``'s guards (and ``_clamp``): only a live tour
    (``tour_state.status == "active"``) reports a parked step — a dismissed,
    completed, or never-started tour returns ``None``. Used by the organic
    ``roster_add_self``/``plan_create`` twins (#441 P3-2) to count a coach who
    takes the action while touring but whose POST carries no ``tour=1`` marker,
    without over-recording when they're parked on some other step.
    """
    profile = CoachProfile.objects.filter(user=user).first()
    if profile is None:
        return None
    state = profile.tour_state or {}
    if state.get("status") != "active":
        return None
    return STEPS[_clamp(state.get("step", 0))]["key"]


def _segment_loaded(user, segment):
    """Whether ``segment``'s data already exists for ``user`` (O7), or ``None``."""
    predicate = _HAS_PREDICATES.get(segment)
    return bool(predicate(user)) if predicate else None


def _sandbox_step_fields(step, user, *, has_demo=False):
    """Resolve one step's sandbox-variant fields.

    Produces the same ``segment``/``action_label``/``signup_gate``/``loaded``
    keys the JS driver has always read; ``action`` (the Phase 3 generic form
    action) is always ``None`` here, so ``resolveActionState`` never takes
    that branch for a sandbox step.

    The ``body`` is state-aware (#441 P2): once a step's segment is loaded it
    swaps to the step's ``body_done`` ("here's the thing you just built");
    a step gated on another segment (``requires_segment`` — ``profile`` needs
    ``athletes`` first) shows its ``body_locked`` prerequisite prompt until
    that data exists; and the signup-gated ``finish`` drops its removal promise
    when the workspace has no demo data to remove (``has_demo``).
    """
    spec = step["sandbox"]
    segment = spec.get("segment")
    loaded = _segment_loaded(user, segment)
    body = spec["body_done"] if (loaded and spec.get("body_done")) else spec["body"]
    requires = spec.get("requires_segment")
    if requires and not _segment_loaded(user, requires):
        body = spec.get("body_locked", body)
    if not has_demo and spec.get("body_no_demo"):
        body = spec["body_no_demo"]
    return {
        "title": spec["title"],
        "body": body,
        "anchor": step["anchor"],
        "segment": segment,
        "action_label": spec.get("action_label"),
        "signup_gate": spec.get("signup_gate", False),
        "action": None,
        "loaded": loaded,
    }


# -- self-variant "is it done?" predicates ------------------------------------
#
# The self mirrors of the sandbox ``demo.has_*`` predicates (which are
# ``is_demo``-scoped by contract, so they can't be reused here). Same shape —
# cheap ``exists()``, derived from data (O7) — but scoped to the coach's own
# self-coaching data: the ``is_self`` link's individual plan, the coach's own
# logs, and their real (non-demo) groups. They give the deliver/results/groups
# self steps the ``loaded`` completion signal the sandbox already had (#441 P3-5).


def _active_self_working_plan(user):
    """The current working plan on the coach's *active* self-link, or ``None``.

    The single source of "the coach's own plan right now". ``working_plan``
    already drops archived plans and group-materialized trees, and ``.active()``
    drops an *ended* self-link — so the deliver/results predicates below never
    read a stale delivered/logged week from a self-link the coach has since
    removed (which ``CoachAthlete.end()`` archives but leaves ``is_self`` set).
    """
    link = CoachAthlete.objects.for_coach(user).active().filter(is_self=True).first()
    return link.working_plan() if link else None


def _self_has_delivery(user):
    """Whether the coach's own current self-link plan has a delivered week.

    Scoped to ``_active_self_working_plan`` (the self mirror of
    ``demo.has_delivery``), so only a delivery on the plan the deliver step is
    actually pointing at counts.
    """
    plan = _active_self_working_plan(user)
    return (
        plan is not None
        and Week.objects.filter(
            mesocycle__plan=plan, delivered_at__isnull=False
        ).exists()
    )


def _self_has_log(user):
    """Whether the coach has *completed* a session on their current self plan.

    The self mirror of ``demo.has_log``, but scoped tighter than the sandbox's
    bare existence (safe there only because the demo log is created ``done`` on
    the coach's own plan): a real coach can hold ``pending`` "save progress" rows
    and — if they're also an athlete under another coach — logs on a foreign
    plan. Neither is a completed result on their current workspace, so gate on a
    ``done`` log on ``_active_self_working_plan`` (matching ``has_plan`` and
    ``_coach_latest_logged_session``'s ``done`` filter).
    """
    plan = _active_self_working_plan(user)
    return (
        plan is not None
        and SessionLog.objects.filter(
            athlete=user,
            status=SessionLog.Status.DONE,
            session__week__mesocycle__plan=plan,
        ).exists()
    )


def _self_has_group(coach):
    """Whether the coach has created a real (non-demo) group (self mirror).

    Mirrors ``demo.has_group`` but non-demo: a demo group (loaded by the sandbox
    ``group`` segment) must not read as the coach's own real group.
    """
    return MesoGroup.objects.filter(coach=coach).exclude(is_demo=True).exists()


def _self_context(user):
    """One-shot facts every dynamic self-variant step needs (computed once per call).

    The welcome/designer/agent steps key off the self-link / working-plan /
    agent-allowance state, and the deliver/results/groups steps off their own
    completion predicates, so all of it is read once per ``build_config`` call
    rather than re-queried per step.
    """
    working_plan = _active_self_working_plan(user)
    return {
        "has_self_link": CoachAthlete.objects.for_coach(user)
        .active()
        .filter(is_self=True)
        .exists(),
        "has_plan": working_plan is not None,
        "has_delivery": _self_has_delivery(user),
        "has_log": _self_has_log(user),
        "has_group": _self_has_group(user),
        "can_use_agent": billing_access.can_use_agent(user),
        # Cheap to resolve unconditionally (no query) — used only once a
        # self-link exists, but harmless to compute either way.
        "plan_create_url": reverse("meso:plan_create", args=[user.pk]),
    }


#: self step key → the ``_self_context`` predicate keying its ``loaded`` flag +
#: "done" copy. These are the action-goal steps whose data-producing action
#: happens on the *real* spotlighted control (no typed tour action button), so
#: they only need a completion signal, not an ``action`` (#441 P3-5).
_SELF_LOADED_BY_STEP = {
    "deliver": "has_delivery",
    "results": "has_log",
    "groups": "has_group",
}

#: self step key → the completion predicate its action-completion advance must
#: gate on. ``plan_deliver`` / ``athlete_log_session`` are also hit when a coach
#: delivers/logs for the athletes they *coach* (not their own self plan), and the
#: logger can save a ``pending`` draft — so the advance may only fire once the
#: coach's *own* step data exists, matching the ``loaded`` display (Codex #441
#: P3-5). Steps whose action can only produce the coach's own data
#: (welcome/designer/groups/agent) need no gate — their endpoint implies it.
_SELF_ADVANCE_PREDICATE = {
    "deliver": _self_has_delivery,
    "results": _self_has_log,
}


def advance_self_step_if_complete(user, step_key):
    """Advance a self-variant action-goal step, gated on its completion predicate.

    Unlike the bare ``advance_if_on_step`` (parked-step check only), this refuses
    to advance until ``step_key``'s own self predicate is satisfied — so a coach
    delivering/logging for *another* athlete they coach, or saving a ``pending``
    log, never skips their own tour past a step whose data doesn't yet exist.
    Steps not in ``_SELF_ADVANCE_PREDICATE`` fall back to the plain advance.

    Self-variant only: the sandbox tour advances exclusively through ``demo_load``
    (its ``program``/``delivery``/``log`` segments), so a sandbox coach who
    happens to deliver/log real data must not move their sandbox tour off a
    segment signal it never loaded.
    """
    if variant_for(user) != "self":
        return False
    predicate = _SELF_ADVANCE_PREDICATE.get(step_key)
    if predicate is not None and not predicate(user):
        return False
    return advance_if_on_step(user, step_key)


def _self_step_fields(step, ctx):
    """Resolve one step's self-variant title/body/action/loaded/anchor (O5).

    The three steps with a real typed *action* branch on ``ctx``:

    - ``welcome``: action always offered (``roster_add_self``); ``loaded`` —
      and the driver's disabled "Done ✓" state — flips once the self-link
      exists.
    - ``designer``: action offered only once the self-link exists (posting
      ``plan_create`` for the coach's own athlete id); ``loaded`` mirrors
      whether that link already has a working plan. No self-link yet → no
      action, and the body swaps to ``locked_body`` ("do step 1 first").
    - ``agent``: action offered only when the self-link exists **and** the
      coach still has agent allowance **and** there's no working plan yet
      (mirrors the roster's "Draft with AI" gate); any other combination
      falls back to the generic ``locked_body``, no ``loaded`` concept.

    The three action-goal steps whose action is taken on the *real* spotlighted
    control — ``deliver``/``results``/``groups`` — carry a data-derived
    ``loaded`` flag (``_SELF_LOADED_BY_STEP``) and swap to their ``body_done``
    once complete, but never offer a typed ``action`` (#441 P3-5). ``profile``
    and ``finish`` stay static copy with no action or ``loaded`` — the driver
    renders an action-less step fine (Next/Back + "Take me there" still work).
    """
    spec = step["self"]
    key = step["key"]
    title = spec["title"]
    body = spec["body"]
    anchor = spec.get("anchor", step["anchor"])
    action = None
    loaded = None

    if key == "welcome":
        loaded = ctx["has_self_link"]
        if loaded and spec.get("body_done"):
            body = spec["body_done"]
        action = {
            "url": reverse("meso:roster_add_self"),
            "label": spec["action_label"],
            "fields": {},
        }
    elif key == "profile":
        # Same prerequisite gate as the sandbox profile step (#441 P2-2): no
        # own row to open until the welcome step adds the coach's self-link.
        if not ctx["has_self_link"]:
            body = spec.get("body_locked", body)
    elif key in _SELF_LOADED_BY_STEP:
        # Action-goal steps with no typed button (the coach uses the real
        # spotlighted control): flip ``loaded`` + swap to done-copy off the
        # step's own completion predicate (#441 P3-5).
        loaded = ctx[_SELF_LOADED_BY_STEP[key]]
        if loaded and spec.get("body_done"):
            body = spec["body_done"]
    elif key == "designer":
        loaded = ctx["has_plan"]
        if ctx["has_self_link"]:
            if loaded and spec.get("body_done"):
                body = spec["body_done"]
            action = {
                "url": ctx["plan_create_url"],
                "label": spec["action_label"],
                "fields": {},
            }
        else:
            body = spec["locked_body"]
    elif key == "agent":
        if not ctx["has_self_link"]:
            body = spec["locked_body_no_link"]
        elif ctx["has_plan"]:
            body = spec["locked_body_has_plan"]
        elif not ctx["can_use_agent"]:
            body = spec["locked_body_no_allowance"]
        else:
            action = {
                "url": ctx["plan_create_url"],
                "label": spec["action_label"],
                "fields": {"draft": "agent"},
            }

    return {
        "title": title,
        "body": body,
        "anchor": anchor,
        "action": action,
        "loaded": loaded,
    }


def _goto_ready_map(user):
    """Per-step "Take me there" readiness — does the target render, or bounce?

    designer/deliver/results redirect back to the roster (an uncorrelated
    flash, then the tour re-offers the same dead-end link — the #441 bounce
    loop) until the coach has a plan / a logged session. Gate the link on
    the exact predicate each view uses; those helpers are identity-blind,
    so one map serves both variants. Steps that target the roster aren't in
    the map (``build_config`` defaults them to ready — the roster never
    dead-ends).

    Deferred import: ``views`` imports this module at the top level, so the
    reverse import must happen at call time (both are loaded once a request
    builds a config).
    """
    from .models import Plan
    from .views import _coach_latest_logged_session
    from .views import _coach_working_plan

    has_editable_plan = (
        _coach_working_plan(user, plans=Plan.objects.editable_by(user)) is not None
    )
    has_working_plan = _coach_working_plan(user) is not None
    has_logged_session = _coach_latest_logged_session(user) is not None
    return {
        "designer": has_editable_plan,
        "agent": has_editable_plan,
        "deliver": has_working_plan,
        "results": has_logged_session,
    }


def build_config(user, variant):
    """The front-end tour config for ``user`` under ``variant``.

    ``variant`` — ``"sandbox"`` or ``"self"`` — is resolved by the caller
    (``variant_for``; O7: derived, never stored), so this stays a pure read
    given the already-known audience. Every step gets its variant's resolved
    title/body/action (``_sandbox_step_fields``/``_self_step_fields``) plus
    the coach's current progress and the endpoints the driver posts to.
    Returns ``None`` if ``user`` has no ``CoachProfile`` to read progress from
    (defensive — callers gate on ``is_active``/``is_touring``/``is_sandbox``
    first, and every sandbox coach has one).
    """
    profile = CoachProfile.objects.filter(user=user).first()
    if profile is None:
        return None
    state = profile.tour_state or {}
    self_ctx = _self_context(user) if variant == "self" else None
    # #441 P2-4: the sandbox finish step's copy branches on whether there's any
    # demo data to remove; resolved once here (has_demo == has_athletes).
    sandbox_has_demo = meso_demo.has_demo(user) if variant == "sandbox" else False
    goto_map = _goto_ready_map(user)
    steps = []
    for step in STEPS:
        fields = (
            _self_step_fields(step, self_ctx)
            if variant == "self"
            else _sandbox_step_fields(step, user, has_demo=sandbox_has_demo)
        )
        steps.append(
            {
                "key": step["key"],
                "title": fields["title"],
                "body": fields["body"],
                "url": reverse(step["url_name"]),
                "anchor": fields["anchor"],
                "segment": fields.get("segment"),
                "action_label": fields.get("action_label"),
                "signup_gate": fields.get("signup_gate", False),
                "action": fields.get("action"),
                "loaded": fields.get("loaded"),
                "goto_ready": goto_map.get(step["key"], True),
            }
        )
    return {
        "steps": steps,
        "variant": variant,
        "step": _clamp(state.get("step", 0)),
        "status": state.get("status", "active"),
        "state_url": reverse("meso:tour_state"),
        "skip_url": reverse("meso:tour_skip"),
        "demo_load_url": reverse("meso:demo_load"),
        "signup_url": reverse("meso:sandbox_signup"),
    }


# ---------------------------------------------------------------------------
# Funnel events (Phase 4, issue #430) — recorded server-side at the tour's own
# endpoints (views.py), never from client-side JS, so an ad blocker can't drop
# them. Deliberately thin wrappers around one shared ``record_event`` rather
# than a generic "record(kind, **kwargs)" call at every call site — each
# wrapper only takes the arguments that event actually has, so a call site
# can't accidentally omit a ``step_key`` a "dismissed" row needs, say.
# ---------------------------------------------------------------------------


def record_event(user, kind, *, variant, step_key="", segment=""):
    """Best-effort funnel-event insert. Cheap (one insert, no reads) and safe.

    Must never break the real action it rides along with (a coach adding
    themselves as an athlete, or a tour step advancing, has to succeed even if
    this write fails). Two layers of protection: the ``transaction.atomic()``
    wraps the insert in its own savepoint, so if it's called from inside a
    caller's own ``atomic()`` block (``plan_create`` wraps its work in one), a
    DB-level failure here only unwinds to that savepoint rather than poisoning
    the caller's outer transaction; the ``except`` around it then swallows
    that (or any other) failure entirely rather than propagating into the
    view. A user with no usable pk (``AnonymousUser``) records with a null
    ``coach`` rather than erroring.
    """
    try:
        with transaction.atomic():
            TourEvent.objects.create(
                coach=user if getattr(user, "pk", None) else None,
                kind=kind,
                variant=variant,
                step_key=step_key,
                segment=segment,
            )
    except Exception:
        logger.warning("Failed to record tour event %r", kind, exc_info=True)


def record_started(user, variant):
    """Tour (re)started — sandbox auto-start, or a real coach's explicit restart."""
    record_event(
        user, TourEvent.Kind.STARTED, variant=variant, step_key=STEPS[0]["key"]
    )


def record_advanced(user, variant, step_key):
    """The tour moved forward onto ``step_key`` (never fired for going back)."""
    record_event(user, TourEvent.Kind.ADVANCED, variant=variant, step_key=step_key)


def record_dismissed(user, variant, step_key):
    """The tour was dismissed while parked on ``step_key``."""
    record_event(user, TourEvent.Kind.DISMISSED, variant=variant, step_key=step_key)


def record_completed(user, variant):
    """The tour was walked to the end and finished (distinct from ``skipped``)."""
    record_event(
        user, TourEvent.Kind.COMPLETED, variant=variant, step_key=STEPS[-1]["key"]
    )


def record_skipped(user, variant, step_key):
    """The O6 "skip · load everything" shortcut fired from ``step_key``."""
    record_event(user, TourEvent.Kind.SKIPPED, variant=variant, step_key=step_key)


def record_opt_in(user, variant, step_key, segment):
    """A step's data action was taken — a sandbox segment load or a self-variant action."""
    record_event(
        user, TourEvent.Kind.OPT_IN, variant=variant, step_key=step_key, segment=segment
    )
