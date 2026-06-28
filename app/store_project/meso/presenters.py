"""Adapt real Meso models into the dict shapes the templates expect.

The roster/profile templates were built against a fixtures module (since
retired). Phase 1 feeds them real, scoped data for everything that exists yet —
the athlete, their training history, and their (global) contraindications.
Program/compliance/activity fields are Phase 2/3 concepts; we pass honest
neutral values (``compliance=None``, ``status=""``, ``has_program=False``) so
the layout holds without inventing numbers.
"""

from collections import defaultdict

from django.urls import reverse
from django.utils import timezone

from .models import Plan
from .models import SessionLog
from .serializers import _fmt_num
from .serializers import _num
from .serializers import current_week
from .serializers import initials
from .serializers import latest_delivered_week
from .serializers import serialize_prescription
from .serializers import serialize_proposed_change


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


def roster_group(group):
    """A row in the coach's roster *Groups* card (groups slice Phase 1).

    Members are scoped to active links (``active_member_users``). There is no
    shared program until groups Phase 2, so the meta line says so rather than
    inventing a current-week label.
    """
    members = group.active_member_users()
    member_objs = [
        {"initials": initials(u.display_name()), "tone": "neutral"} for u in members
    ]
    count = len(member_objs)
    focus = group.focus or "General"
    meta = " · ".join(
        [
            f"{count} participant{'' if count == 1 else 's'}",
            focus,
            "No shared program yet",
        ]
    )
    return {
        "id": group.pk,
        "name": group.name,
        "focus": focus,
        "member_objs": member_objs,
        "meta": meta,
        "status_label": group.get_status_display(),
    }


def group_detail(group):
    """The group detail page: members + their cross-group contraindication flags.

    The "flags across group" set folds every active member's contraindication
    labels into one unique, sorted list (the designer prototype's panel).
    """
    members = group.active_member_users()
    member_data = []
    flags = set()
    for user in members:
        labels = [c.label for c in _active_contraindications(user)]
        flags.update(labels)
        name = user.display_name()
        member_data.append(
            {
                "id": user.pk,
                "name": name,
                "initials": initials(name),
                "tone": "neutral",
                "flags": labels,
            }
        )
    # The shared program (Phase 2): its id when the group already has one (the
    # detail page links straight to the designer), else None (offer to design it).
    shared = group.shared_plan()
    return {
        "id": group.pk,
        "name": group.name,
        "focus": group.focus or "General",
        "status_label": group.get_status_display(),
        "members": member_data,
        "member_count": len(member_data),
        "flags": sorted(flags),
        "shared_plan_id": shared.pk if shared else None,
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


# -- session results (athlete slice Phase 3) -------------------------------
#
# The coach's results screen, off real logs (``mockdata.RESULTS_*`` retired):
# the athlete's most recent ``SessionLog`` for a session, scored against the
# prescribed targets — completion, RPE vs target, and the flags that drive the
# "adjust next week" hand-off to the agent. The same logged truth the agent
# grounds on (``serialize_recent_logs``), now shown to the coach.

# A logged set running this many RPE points over target is worth acting on — it
# becomes a "flag" (the row still lights up for *any* overshoot).
RPE_FLAG_THRESHOLD = 1.0


def _results_target_label(prescription, unit):
    """The prescribed target, e.g. "3×6 @ 70 kg · RPE 7" (no unit for "BW")."""
    label = f"{prescription.sets or '—'}×{prescription.reps or '—'}"
    if prescription.load:
        suffix = f" {unit}" if _num(prescription.load) is not None else ""
        label += f" @ {prescription.load}{suffix}"
    if prescription.rpe and prescription.rpe != "—":
        label += f" · RPE {prescription.rpe}"
    return label


def _logged_label(logged_sets, unit):
    """What the athlete did, e.g. "2×12, 1×9 @ 41 kg" (reps grouped, load suffixed).

    Equal-rep runs collapse to ``count×reps``; a uniform load is appended once,
    a varying numeric load as a range. A non-numeric load ("BW") carries no unit.
    """
    groups = []  # [count, reps] runs, in logged order
    for s in logged_sets:
        reps = s.reps or "—"
        if groups and groups[-1][1] == reps:
            groups[-1][0] += 1
        else:
            groups.append([1, reps])
    label = ", ".join(f"{count}×{reps}" for count, reps in groups)
    loads = [s.load for s in logged_sets if s.load]
    if loads and all(x == loads[0] for x in loads):
        suffix = f" {unit}" if _num(loads[0]) is not None else ""
        label += f" @ {loads[0]}{suffix}"
    elif loads and all(_num(x) is not None for x in loads):
        nums = [_num(x) for x in loads]
        label += f" @ {_fmt_num(min(nums))}–{_fmt_num(max(nums))} {unit}"
    return label


def _worst_rep_shortfall(prescription, logged_sets):
    """The biggest rep miss vs the prescribed reps, as ``(deficit, set_number)``.

    Only meaningful when the prescribed reps are a plain number (not "AMRAP" /
    "8-10"); returns None otherwise, or when every logged set met the target.
    Catches the case a set-count check misses — all sets done, but reps fell
    short on one (e.g. a 3×12 logged as 12, 12, 9).
    """
    target_reps = _num(prescription.reps)
    if target_reps is None:
        return None
    worst = None
    for s in logged_sets:
        reps = _num(s.reps)
        if reps is None or reps >= target_reps:
            continue
        deficit = target_reps - reps
        if worst is None or deficit > worst[0]:
            worst = (deficit, s.set_number)
    return worst


def _exercise_result(prescription, logged_sets, unit):
    """One results row + its RPE overshoot (None when not comparable).

    The row mirrors the prototype's columns (target / logged / RPE / note); the
    RPE shown is the *hardest* logged set, and ``rpe_state`` lights "over" on any
    overshoot. The note is the most actionable fact we can derive, in order: a
    set shortfall, a rep shortfall (the prescribed reps missed on a set), then a
    meaningful RPE overshoot.
    """
    target_rpe = _num(prescription.rpe)
    logged_rpes = [_num(s.rpe) for s in logged_sets if _num(s.rpe) is not None]
    top_rpe = max(logged_rpes) if logged_rpes else None
    overshoot = (
        top_rpe - target_rpe if top_rpe is not None and target_rpe is not None else None
    )
    prescribed_n = _prescribed_set_count(prescription.sets)
    logged_n = len(logged_sets)
    rep_short = _worst_rep_shortfall(prescription, logged_sets)
    if prescribed_n and 0 < logged_n < prescribed_n:
        note = f"{logged_n}/{prescribed_n} sets logged"
    elif rep_short is not None:
        deficit, set_number = rep_short
        plural = "s" if deficit != 1 else ""
        note = f"missed {_fmt_num(deficit)} rep{plural} on set {set_number}"
    elif overshoot is not None and overshoot >= RPE_FLAG_THRESHOLD:
        note = f"RPE {_fmt_num(top_rpe)} over target"
    else:
        note = ""
    row = {
        "name": prescription.name,
        "target": _results_target_label(prescription, unit),
        "logged": _logged_label(logged_sets, unit) if logged_sets else "—",
        "rpe": _fmt_num(top_rpe) if top_rpe is not None else "—",
        "rpe_state": "over" if overshoot is not None and overshoot > 0 else "on",
        "note": note,
    }
    return row, overshoot


def _avg_rpe_delta(prescriptions, sets_by_prescription):
    """Mean (logged − target) RPE across comparable sets, signed; "—" if none."""
    deltas = []
    for prescription in prescriptions:
        target = _num(prescription.rpe)
        if target is None:
            continue
        for s in sets_by_prescription.get(prescription.pk, []):
            logged = _num(s.rpe)
            if logged is not None:
                deltas.append(logged - target)
    if not deltas:
        return "—"
    avg = sum(deltas) / len(deltas)
    return "0.0" if round(avg, 1) == 0 else f"{avg:+.1f}"


def _session_label(session):
    week = session.week
    label = f"Wk {week.index} · Day {session.day_number}"
    return f"{label} — {session.name}" if session.name else label


def _logged_date(log):
    if log is None or log.date is None:
        return None
    return f"{log.date:%a, %b} {log.date.day}"


def session_results(session):
    """The coach's results screen for one session, off the athlete's real log.

    Reads the athlete's most recent *done* ``SessionLog`` for ``session`` and
    scores its sets against the prescribed targets. A pending draft (the athlete
    hit "Save progress" but hasn't finished) is not feedback yet, so it — like an
    unlogged session — renders an honest awaiting state (targets only, 0%
    complete) rather than inventing numbers. ``session`` arrives coach-scoped
    with its prescriptions prefetched.
    """
    plan = session.week.mesocycle.plan
    athlete = plan.athlete
    prescriptions = list(session.prescriptions.all())
    log = (
        SessionLog.objects.filter(
            session=session, athlete=athlete, status=SessionLog.Status.DONE
        )
        .order_by("-date", "-created_at")
        .prefetch_related("sets")
        .first()
    )
    sets_by_prescription = defaultdict(list)
    if log is not None:
        for s in log.sets.all():
            if s.prescription_id is not None:
                sets_by_prescription[s.prescription_id].append(s)

    results = [
        _exercise_result(p, sets_by_prescription.get(p.pk, []), plan.unit)
        for p in prescriptions
    ]
    rows = [row for row, _ in results]

    # Completion = logged sets / prescribed sets. A free-form set cell ("AMRAP",
    # "3-4") has no integer target, so fall back to what was logged for that row
    # — it neither divides by zero nor skews the ratio with an empty denominator.
    prescribed_total = 0
    logged_total = 0
    for p in prescriptions:
        logged_n = len(sets_by_prescription.get(p.pk, []))
        prescribed_total += _prescribed_set_count(p.sets) or logged_n
        logged_total += logged_n
    completion = (
        min(round(100 * logged_total / prescribed_total), 100)
        if prescribed_total
        else 0
    )

    flagged = [
        (row, o) for row, o in results if o is not None and o >= RPE_FLAG_THRESHOLD
    ]
    if flagged:
        worst_row, worst_over = max(flagged, key=lambda pair: pair[1])
        flag = (
            f"{worst_row['name']} ran {_fmt_num(worst_over)} RPE over target "
            "— consider holding load next session."
        )
    else:
        flag = ""

    return {
        "athlete": {"name": athlete.display_name()},
        "plan_id": plan.pk,
        "rows": rows,
        "summary": {
            "session": _session_label(session),
            "logged": _logged_date(log),
            "completion": completion,
            "avg_rpe_delta": _avg_rpe_delta(prescriptions, sets_by_prescription),
            "flag": flag,
            "flag_count": len(flagged),
            "logged_state": log is not None,
        },
    }


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
