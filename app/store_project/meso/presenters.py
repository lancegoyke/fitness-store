"""Adapt real Meso models into the dict shapes the templates expect.

The roster/profile templates were built against ``mockdata.py``. Phase 1 feeds
them real, scoped data for everything that exists yet — the athlete, their
training history, and their (global) contraindications. Program/compliance/
activity fields are Phase 2/3 concepts; we pass honest neutral values
(``compliance=None``, ``status=""``, ``has_program=False``) so the layout holds
without inventing numbers.
"""

from django.urls import reverse
from django.utils import timezone

from .models import Plan
from .models import SessionLog
from .serializers import current_week
from .serializers import latest_delivered_week
from .serializers import serialize_prescription
from .serializers import serialize_proposed_change


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


def review_changes(batch):
    """Context for the review screen from a real ``AgentProposalBatch`` (B6).

    Feeds the same template the prototype fixtures did, so a real batch renders
    unchanged; per-change approve/reject persistence + apply land in Phase 2.
    """
    return {
        "athlete": {"name": batch.plan.athlete.display_name()},
        "changes": [serialize_proposed_change(c) for c in batch.changes.all()],
    }


def coach_style(coach):
    """The current coach's programming voice, for the profile left rail."""
    profile = getattr(coach, "coach_profile", None)
    if profile is None:
        return {"tags": [], "avoid": ""}
    return {"tags": profile.programming_style or [], "avoid": profile.avoid_rules}


# -- athlete surface (athlete slice Phase 1) -------------------------------
#
# The athlete's *own* read view (distinct from the coach's view of an athlete).
# Scoped to delivered weeks across the athlete's active coaches; an undelivered
# week never reaches these presenters (the view filters first). Log status comes
# only from the athlete's *own* ``SessionLog`` rows.


def _done_session_ids(session_ids, athlete):
    """Which of ``session_ids`` the athlete has a *done* log for (one query)."""
    return set(
        SessionLog.objects.filter(
            session_id__in=session_ids,
            athlete=athlete,
            status=SessionLog.Status.DONE,
        ).values_list("session_id", flat=True)
    )


def _athlete_session_row(session, *, done):
    """One session in the athlete's week — a tappable row on the home screen."""
    status = "done" if done else "pending"
    return {
        "id": session.pk,
        "n": session.day_number,
        "name": session.name,
        "bias": session.bias,
        # ``prescriptions`` is prefetched on the home query — no per-row query.
        "exercise_count": len(session.prescriptions.all()),
        "status": status,
        "status_label": "Logged" if done else "To do",
        "url": reverse("meso:athlete_session", kwargs={"pk": session.pk}),
    }


def athlete_home(user):
    """The athlete's active programs, each with its latest delivered week.

    One card per non-archived plan across the athlete's *active* coaches (D-a).
    A plan with no delivered week is shown as awaiting; otherwise its delivered
    sessions render with the athlete's own done/pending status.
    """
    plans = (
        Plan.objects.for_athlete(user)
        .exclude(status=Plan.Status.ARCHIVED)
        .select_related("relationship__coach")
        .order_by("-modified")
    )
    cards = []
    for plan in plans:
        week = latest_delivered_week(plan)
        sessions = []
        if week is not None:
            session_objs = list(week.sessions.prefetch_related("prescriptions"))
            done = _done_session_ids([s.pk for s in session_objs], user)
            sessions = [
                _athlete_session_row(s, done=s.pk in done) for s in session_objs
            ]
        cards.append(
            {
                "id": plan.pk,
                "title": plan.title,
                "goal": plan.goal,
                "coach": plan.coach.display_name(),
                "block": week.mesocycle.name if week else "",
                "week": f"Wk {week.index}" if week else "",
                "delivered_at": week.delivered_at if week else None,
                "sessions": sessions,
                "awaiting": week is None,
            }
        )
    return cards


def _prescribed_set_count(sets_text):
    """How many set rows a prescription's ``sets`` cell asks for, or 0.

    ``sets`` is free text — "3" is a plain count, but "3-4"/"AMRAP" aren't. We
    only expand a plain integer; the caller falls back to a default otherwise.
    """
    try:
        return max(int(str(sets_text).strip()), 0)
    except (TypeError, ValueError):
        return 0


def _set_rows(prescription, logged, *, default=3, cap=12, hard_cap=60):
    """Pre-filled set-input rows for one prescription (Phase 2 logger).

    ``logged`` maps ``(prescription_id, set_number)`` to the athlete's own
    ``LoggedSet``. The row count is the prescribed sets (capped, or ``default``
    when the cell is free-form), widened to show every set the athlete already
    logged so a reload never hides logged data — but ``hard_cap`` bounds the
    render unconditionally so a stray large ``set_number`` can never balloon the
    page (the log endpoint also rejects set numbers above its own ceiling).
    """
    prescribed = _prescribed_set_count(prescription.sets) or default
    logged_numbers = [n for (pid, n) in logged if pid == prescription.pk]
    count = max(min(prescribed, cap), max(logged_numbers, default=0), 1)
    count = min(count, hard_cap)
    rows = []
    for n in range(1, count + 1):
        s = logged.get((prescription.pk, n))
        rows.append(
            {
                "set_number": n,
                "reps": s.reps if s else "",
                "load": s.load if s else "",
                "rpe": s.rpe if s else "",
                "done": s is not None,
            }
        )
    return rows


def _target_label(prescription):
    """The prescribed target shown above a logger's set rows.

    Carries the coach's full prescription — sets×reps plus load and RPE when set
    — so the athlete sees what to aim for before entering what they did, e.g.
    "3 × 6 · 70 · RPE 7".
    """
    parts = [f"{prescription.sets or '—'} × {prescription.reps or '—'}"]
    if prescription.load:
        parts.append(prescription.load)
    if prescription.rpe:
        parts.append(f"RPE {prescription.rpe}")
    return " · ".join(parts)


def athlete_session(session, athlete):
    """One delivered session as the athlete's interactive logger (Phase 2).

    ``session`` is already athlete-scoped + delivered by the view; this formats
    the prescribed grid into set-input rows, pre-filled from the athlete's own
    most-recent ``SessionLog``, and reports its done status. Prescriptions are
    prefetched on the view query.
    """
    log = (
        SessionLog.objects.filter(session=session, athlete=athlete)
        .order_by("-created_at")
        .prefetch_related("sets")
        .first()
    )
    logged = (
        {(s.prescription_id, s.set_number): s for s in log.sets.all()} if log else {}
    )
    done = log is not None and log.status == SessionLog.Status.DONE
    week = session.week
    return {
        "id": session.pk,
        "n": session.day_number,
        "name": session.name,
        "bias": session.bias,
        "status": "done" if done else "pending",
        "status_label": "Logged" if done else "To do",
        "block": week.mesocycle.name,
        "week": f"Wk {week.index}",
        "plan_title": week.mesocycle.plan.title,
        "notes": log.notes if log else "",
        "log_url": reverse("meso:athlete_log_session", kwargs={"pk": session.pk}),
        "exercises": [
            {
                **serialize_prescription(p),
                "target": _target_label(p),
                "set_rows": _set_rows(p, logged),
            }
            for p in session.prescriptions.all()
        ],
    }


def athlete_log_payload(session_ctx):
    """The JSON the Alpine logger hydrates from (and POSTs back).

    A trimmed view of ``athlete_session``: just what the client needs to render
    the set rows and submit them — the log URL, current status, and per-exercise
    rows. Kept separate from the display dict so the template's ``json_script``
    payload stays small and intentional.
    """
    return {
        "log_url": session_ctx["log_url"],
        "status": session_ctx["status"],
        "exercises": [
            {
                "id": e["id"],
                "name": e["name"],
                "target": e["target"],
                "note": e.get("note", ""),
                "tag": e.get("tag", ""),
                "set_rows": e["set_rows"],
            }
            for e in session_ctx["exercises"]
        ],
    }
