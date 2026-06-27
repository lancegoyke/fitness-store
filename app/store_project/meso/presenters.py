"""Adapt real Meso models into the dict shapes the templates expect.

The roster/profile templates were built against ``mockdata.py``. Phase 1 feeds
them real, scoped data for everything that exists yet — the athlete, their
training history, and their (global) contraindications. Program/compliance/
activity fields are Phase 2/3 concepts; we pass honest neutral values
(``compliance=None``, ``status=""``, ``has_program=False``) so the layout holds
without inventing numbers.
"""

from django.utils import timezone

from .serializers import current_week


def initials(name):
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _age(user):
    birthday = getattr(user, "birthday", None)
    if not birthday:
        return None
    today = timezone.localdate()
    return (
        today.year
        - birthday.year
        - ((today.month, today.day) < (birthday.month, birthday.day))
    )


def _training_label(user):
    profile = getattr(user, "athlete_profile", None)
    months = profile.training_months if profile else None
    if months is None:
        return None
    if months < 12:
        return f"{months} mo training"
    years, rem = divmod(months, 12)
    if rem:
        return f"{years} yr {rem} mo training"
    return f"{years} yr training"


def _active_contraindications(user):
    # ``contraindications`` is prefetched on the roster/profile querysets.
    return [c for c in user.contraindications.all() if c.active]


def roster_athlete(user):
    """A row in the coach's roster list."""
    name = user.display_name()
    meta_parts = [p for p in [_training_label(user)] if p]
    return {
        "id": user.pk,
        "name": name,
        "initials": initials(name),
        "tone": "neutral",
        "meta": " · ".join(meta_parts) or "No training history on file",
        "flags": [c.label for c in _active_contraindications(user)],
        # Phase 2 (program/agent) and Phase 3 (logs) — hidden until they exist.
        "compliance": None,
        "status": "",
        "status_label": "",
    }


def profile_athlete(user):
    """The expanded athlete record behind the roster row."""
    name = user.display_name()
    subtitle_parts = [str(p) for p in [_age(user), _training_label(user)] if p]
    return {
        "id": user.pk,
        "name": name,
        "initials": initials(name),
        "tone": "neutral",
        "subtitle": " · ".join(subtitle_parts) or "No training history on file",
        # Goals are per-plan (D-b); they arrive with the program schema (Phase 2).
        "goals": [],
        "contraindications": [c.text for c in _active_contraindications(user)],
        "has_program": False,
        "compliance": None,
        "status": "",
        "status_label": "",
    }


def deliver_screen(plan):
    """Context for the plan-bound deliver screen (Phase 4).

    Real athlete + current-week summary. Scheduling and the full "changes since
    last delivery" diff are later-slice concerns (notifications / agent), so the
    template hides those controls in plan mode; we only surface whether this is
    a first delivery or a re-delivery.
    """
    week = current_week(plan)
    mesocycle = week.mesocycle if week else None
    session_count = week.sessions.count() if week else 0
    is_redelivery = week is not None and week.deliveries.exists()

    athlete = profile_athlete(plan.athlete)
    athlete["block"] = mesocycle.name if mesocycle else ""
    athlete["week"] = f"Wk {week.index}" if week else ""
    return {
        "athlete": athlete,
        "deliver": {
            "what": plan.title,
            "sessions": session_count,
            "is_redelivery": is_redelivery,
        },
    }


def coach_style(coach):
    """The current coach's programming voice, for the profile left rail."""
    profile = getattr(coach, "coach_profile", None)
    if profile is None:
        return {"tags": [], "avoid": ""}
    return {"tags": profile.programming_style or [], "avoid": profile.avoid_rules}
