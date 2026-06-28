"""Serialize a real ``Plan`` into the shape the Meso designer renders.

The designer (``static/js/meso.js``) keeps three in-memory arrays:

- ``program`` — the current week's sessions, each with its exercise rows;
- ``weeks``   — the current mesocycle's week strip (volume/intensity bars);
- ``phases``  — the macrocycle rail (one entry per mesocycle).

``serialize_plan`` reproduces that shape from the ``Plan → Mesocycle → Week →
Session → ExercisePrescription`` hierarchy so Phase 3 can hydrate the designer
from the database instead of fixtures. The designer's ``last`` column (what the
athlete actually did last time, per lift) is derived from real logged sets here
(athlete slice Phase 3); ``adj`` (a group-only agent overlay) arrives with the
groups slice (S1, out of scope) and is still not emitted.
"""

from collections import Counter
from collections import defaultdict

from django.urls import reverse

from . import models


def serialize_prescription(prescription):
    """One exercise row in a session's grid."""
    data = {
        "id": prescription.pk,
        "name": prescription.name,
        "sets": prescription.sets,
        "reps": prescription.reps,
        "load": prescription.load,
        "rpe": prescription.rpe,
        "note": prescription.note,
    }
    # The designer renders a single `tag`; the model stores a list.
    if prescription.tags:
        data["tag"] = prescription.tags[0]
    return data


def serialize_proposed_change(change):
    """One agent-proposed edit, in the shape the review screen renders.

    Matches the prototype's ``PROPOSED_CHANGES`` dicts (``id``/``kind``/``day``/
    ``title``/``before``/``after``/``rationale``/``honors``) so the same template
    renders real batches; ``status`` is added for the (Phase 2) approve gate.
    """
    return {
        "id": change.pk,
        "kind": change.get_kind_display(),
        "day": change.day_label,
        "title": change.title,
        "before": change.before,
        "after": change.after,
        "rationale": change.rationale,
        "honors": change.honors,
        "status": change.status,
    }


# A change-less, summary-less agent reply still says *something* so the bubble
# never renders blank — mirrors ``meso.js``'s ``batchMessage`` fallback.
_NO_CHANGES_NOTE = (
    "I couldn't find any safe changes to propose for that. "
    "Try rephrasing or adjusting the plan directly."
)
_DRAFTING_NOTE = "Still working on this proposal…"
_FAILED_NOTE = "The agent had trouble responding. Give it another try."


def _agent_reply_for_batch(batch):
    """The agent's side of one batch, in the ``meso.js`` message shape.

    The agent never sends free-form chat — its reply is exactly the batch's
    outcome: a failure note, a still-drafting note, or a summary plus the inline
    proposed changes (with a review link when there are any).
    """
    Status = models.AgentProposalBatch.Status
    message = {"id": f"agent-{batch.pk}", "role": "agent"}

    if batch.status == Status.FAILED:
        message["text"] = batch.error or _FAILED_NOTE
        message["error"] = True
        return message
    if batch.status == Status.DRAFTING:
        # A run still in flight at render time. Carry the status URL so the
        # front-end can resume polling and replace this placeholder when the
        # batch lands; the note is the fallback if the run never resolves.
        message["text"] = _DRAFTING_NOTE
        message["pollUrl"] = reverse(
            "meso:api_batch_status", kwargs={"batch_id": batch.pk}
        )
        return message

    changes = [serialize_proposed_change(c) for c in batch.changes.all()]
    message["text"] = batch.summary or (_NO_CHANGES_NOTE if not changes else "")
    message["changes"] = changes
    message["reviewUrl"] = (
        reverse("meso:review_batch", kwargs={"batch_id": batch.pk}) if changes else None
    )
    return message


def serialize_chat_thread(plan):
    """The designer's persisted agent conversation, oldest message first.

    Every coach turn is an ``AgentProposalBatch`` (``instruction`` = the coach's
    message; ``summary`` + ``ProposedChange`` rows = the agent's reply), so the
    plan's batches *are* the thread — no separate chat model. Each batch expands
    to a coach message then an agent message, in the exact shape ``meso.js``'s
    ``messages`` array renders, so the front-end hydrates without remapping.
    """
    batches = plan.proposal_batches.order_by("created_at", "pk").prefetch_related(
        "changes"
    )
    thread = []
    for batch in batches:
        thread.append(
            {"id": f"coach-{batch.pk}", "role": "coach", "text": batch.instruction}
        )
        thread.append(_agent_reply_for_batch(batch))
    return thread


def serialize_session(session):
    """One training day (a column in the designer grid)."""
    return {
        "id": session.pk,
        "n": session.day_number,
        "name": session.name,
        "bias": session.bias,
        "exercises": [serialize_prescription(p) for p in session.prescriptions.all()],
    }


def serialize_week(week):
    """One column in the designer's week strip."""
    return {
        "label": f"Wk {week.index}",
        "phase": week.phase,
        "vol": week.volume,
        "inten": week.intensity,
        "deload": week.is_deload,
        "current": week.is_current,
    }


def serialize_mesocycle(mesocycle, state):
    """One bar in the macrocycle rail."""
    return {
        "name": mesocycle.name,
        "weeks": f"{mesocycle.week_count} wk",
        "state": state,
    }


def _phase_states(mesocycles, current_mesocycle):
    """Map each mesocycle to done/current/next/future by *sequence position*.

    Position, not ``order`` arithmetic: the model enforces unique — not
    contiguous — ``order``, so reordering/deleting blocks can leave gaps. The
    block immediately following the current one is always ``next``.
    """
    current_index = next(
        (
            i
            for i, m in enumerate(mesocycles)
            if current_mesocycle is not None and m.pk == current_mesocycle.pk
        ),
        None,
    )
    states = []
    for i in range(len(mesocycles)):
        if current_index is None or i > current_index + 1:
            states.append("future")
        elif i < current_index:
            states.append("done")
        elif i == current_index:
            states.append("current")
        else:  # i == current_index + 1
            states.append("next")
    return states


def serialize_week_snapshot(week):
    """A self-contained snapshot of a week, for a ``WeekDelivery`` payload.

    Captures the week's meta plus its full session/prescription grid so a later
    delivery can diff against it ("changes since last delivery").
    """
    return {
        "week": {
            "id": week.pk,
            "index": week.index,
            "phase": week.phase,
            "volume": week.volume,
            "intensity": week.intensity,
            "is_deload": week.is_deload,
        },
        "sessions": [
            serialize_session(s)
            for s in week.sessions.prefetch_related("prescriptions")
        ],
    }


def serialize_session_log(log):
    """The athlete's saved log for a session, in the shape the log endpoint returns.

    Echoes back what was persisted (status/date/notes + the logged sets) so the
    athlete's logger can confirm the write and the page can re-hydrate on reload.
    """
    return {
        "id": log.pk,
        "status": log.status,
        "date": log.date.isoformat() if log.date else None,
        "notes": log.notes,
        "sets": [
            {
                "id": s.pk,
                "prescription": s.prescription_id,
                "set_number": s.set_number,
                "reps": s.reps,
                "load": s.load,
                "rpe": s.rpe,
            }
            for s in log.sets.order_by("set_number")
        ],
    }


def serialize_recent_logs(plan, *, limit=5, sets_cap=24):
    """A compact summary of the athlete's most recent logged sessions on this plan.

    Grounds the agent (Phase 4) in what the athlete actually did — newest first —
    so a progression/deload proposal can anchor on logged loads, not just the
    prescribed grid. Scoped to the plan's athlete and this plan's sessions, and
    capped (``limit`` sessions, ``sets_cap`` sets each) to keep the context small.
    """
    logs = (
        models.SessionLog.objects.filter(
            session__week__mesocycle__plan=plan, athlete=plan.athlete
        )
        .select_related("session")
        .prefetch_related("sets__prescription")
        .order_by("-date", "-created_at")[:limit]
    )
    summary = []
    for log in logs:
        summary.append(
            {
                "date": log.date.isoformat() if log.date else None,
                "session": str(log.session),
                "status": log.status,
                "sets": [
                    {
                        "exercise": s.prescription.name if s.prescription else "",
                        "set": s.set_number,
                        "reps": s.reps,
                        "load": s.load,
                        "rpe": s.rpe,
                    }
                    for s in list(log.sets.all())[:sets_cap]
                ],
            }
        )
    return summary


# -- the designer's "last time" column (athlete slice Phase 3) -------------
#
# Each prescription in the week being designed carries a compact ``last`` —
# what the athlete actually did the most recent time they logged that lift — so
# the coach sets loads against real performance, not just the prescribed grid.
# The same logged truth the agent grounds on (``serialize_recent_logs``).


def _num(value):
    """``value`` as a float, or None when it isn't a plain number ("BW", "—")."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_num(value):
    """A number without a trailing ``.0`` (70.0 → "70", 72.5 → "72.5")."""
    f = float(value)
    return str(int(f)) if f == int(f) else str(f)


def _exercise_key(exercise_id, name):
    """Identity for matching a logged set to a prescription across sessions.

    A catalog-linked lift matches by FK; a free-text one by case-folded name —
    so last week's "Box Squat" surfaces against this week's "Box Squat".
    """
    if exercise_id is not None:
        return ("id", exercise_id)
    return ("name", (name or "").strip().lower())


def _mode(values):
    """The most common value, ties broken by first appearance (or None)."""
    present = [v for v in values if v not in (None, "")]
    if not present:
        return None
    return Counter(present).most_common(1)[0][0]


def _summarize_last_sets(logged_sets, unit):
    """A compact one-line summary of one lift's logged sets, e.g. "3×6 · 70kg · RPE8".

    Collapses the typical (uniform) working set to ``{n}×{reps} · {load}{unit}``
    — modal reps/load, the hardest set's RPE — matching the prototype's badge.
    The unit suffix is dropped for a non-numeric load ("BW").
    """
    n = len(logged_sets)
    reps = _mode([s.reps for s in logged_sets]) or "—"
    parts = [f"{n}×{reps}"]
    load = _mode([s.load for s in logged_sets])
    if load is not None:
        parts.append(f"{load}{unit}" if _num(load) is not None else load)
    rpes = [_num(s.rpe) for s in logged_sets if _num(s.rpe) is not None]
    if rpes:
        parts.append(f"RPE{_fmt_num(max(rpes))}")
    return " · ".join(parts)


def last_logged_labels(plan, prescriptions, unit):
    """Map each prescription's pk to a "last time" label from the athlete's logs.

    For every rendered prescription, find the athlete's most recent *completed*
    logged sets for that lift (by exercise identity) anywhere on this plan and
    summarize them. Only ``DONE`` logs count — a pending "Save progress" draft is
    a partial session, not the athlete's last performance (the results screen
    treats it the same way). One query over the plan's logged sets — no per-row
    lookups; a lift the athlete has never logged is simply absent from the map.
    """
    target_keys = {p.pk: _exercise_key(p.exercise_id, p.name) for p in prescriptions}
    wanted = set(target_keys.values())
    if not wanted:
        return {}
    logged_sets = (
        models.LoggedSet.objects.filter(
            session_log__session__week__mesocycle__plan=plan,
            session_log__athlete=plan.athlete,
            session_log__status=models.SessionLog.Status.DONE,
        )
        .select_related("session_log", "prescription")
        .order_by("-session_log__date", "-session_log__created_at", "set_number")
    )
    # ``logged_sets`` is newest-log-first; the first log that mentions a lift is
    # its most recent, and we collect only that log's sets for the lift.
    best_log = {}
    sets_by_key = defaultdict(list)
    for ls in logged_sets:
        if ls.prescription is None:
            continue
        key = _exercise_key(ls.prescription.exercise_id, ls.prescription.name)
        if key not in wanted:
            continue
        best_log.setdefault(key, ls.session_log_id)
        if best_log[key] == ls.session_log_id:
            sets_by_key[key].append(ls)
    return {
        pk: _summarize_last_sets(sets_by_key[key], unit)
        for pk, key in target_keys.items()
        if sets_by_key.get(key)
    }


def latest_delivered_week(plan):
    """The most recently delivered week of ``plan``, or None.

    Delivery gates *visibility*: a week the coach has delivered
    (``Week.delivered_at`` stamped by ``plan_deliver``) becomes visible to the
    athlete; an undelivered week is not. The athlete then sees that week's
    **current** (live) contents — a coach correcting an already-delivered week
    is reflected, by design; the frozen ``WeekDelivery`` snapshot is the
    historical record for the (deferred) "changes since last delivery" diff, not
    a separate athlete-facing view. Newest delivery wins so the athlete lands on
    the week their coach just sent. See ``docs/meso/athlete-plan.md``.
    """
    return (
        models.Week.objects.filter(mesocycle__plan=plan, delivered_at__isnull=False)
        .select_related("mesocycle")
        .order_by("-delivered_at")
        .first()
    )


def current_week(plan, week=None):
    """The week the designer opens to.

    An explicit ``week`` wins; otherwise the flagged current week, or — failing
    both — the earliest week in the plan.
    """
    if week is not None:
        return week
    weeks = list(
        models.Week.objects.filter(mesocycle__plan=plan)
        .select_related("mesocycle")
        .order_by("mesocycle__order", "index")
    )
    for candidate in weeks:
        if candidate.is_current:
            return candidate
    return weeks[0] if weeks else None


def serialize_plan(plan, week=None):
    """Serialize ``plan`` to the designer's ``program``/``weeks``/``phases`` shape.

    ``week`` optionally pins which week populates ``program``/``weeks``;
    otherwise the flagged current week (or the plan's first) is used.
    """
    open_week = current_week(plan, week)
    current_mesocycle = open_week.mesocycle if open_week else None

    if open_week is not None:
        sessions = list(open_week.sessions.prefetch_related("prescriptions"))
        program = [serialize_session(s) for s in sessions]
        # Light up the "last time" column from real logs (Phase 3): one query
        # over the plan's logged sets, mapped onto the rendered prescriptions.
        prescriptions = [p for s in sessions for p in s.prescriptions.all()]
        last_map = last_logged_labels(plan, prescriptions, plan.unit)
        for session_data in program:
            for exercise in session_data["exercises"]:
                label = last_map.get(exercise["id"])
                if label:
                    exercise["last"] = label
        week_strip = [serialize_week(w) for w in current_mesocycle.weeks.all()]
    else:
        program = []
        week_strip = []

    mesocycles = list(plan.mesocycles.all())
    states = _phase_states(mesocycles, current_mesocycle)
    phases = [serialize_mesocycle(m, s) for m, s in zip(mesocycles, states)]

    return {
        "plan": {
            "id": plan.pk,
            "title": plan.title,
            "goal": plan.goal,
            "status": plan.status,
            "unit": plan.unit,
        },
        "program": program,
        "weeks": week_strip,
        "phases": phases,
    }
