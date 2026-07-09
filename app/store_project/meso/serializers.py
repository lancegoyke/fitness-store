"""Serialize a real ``Plan`` into the shape the Meso designer renders.

The designer (``static/js/meso.js``) keeps three in-memory arrays:

- ``program`` — the current week's sessions, each with its exercise rows;
- ``weeks``   — the current mesocycle's week strip (volume/intensity bars);
- ``phases``  — the macrocycle rail (one entry per mesocycle).

``serialize_plan`` reproduces that shape from the ``Plan → Mesocycle → Week →
Session → Prescription`` hierarchy (a ``Prescription`` is a cell = the fixed
``ExerciseSlot`` row × this ``Week``) so Phase 3 can hydrate the designer
from the database instead of fixtures. The designer's ``last`` column (what the
athlete actually did last time, per lift) is derived from real logged sets here
(athlete slice Phase 3); ``adj`` (a group plan's per-row per-athlete auto-adjust
badge) is emitted from real ``PrescriptionOverride`` diffs (groups slice S1,
Phase 3 — ``group_adjustments``).
"""

from collections import Counter
from collections import defaultdict

from django.urls import reverse

from . import models


def initials(name):
    """Two-letter monogram for an avatar ("Maya Okonkwo" → "MO")."""
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def serialize_prescription(cell):
    """One exercise row in a session's grid.

    ``cell`` is a ``Prescription`` — a fixed ``ExerciseSlot`` row × this
    ``Week``. Its resolving properties (``name``/``tags``) already fold in a
    one-week swap, so this body reads unchanged from the old per-week model.
    """
    data = {
        "id": cell.pk,
        "name": cell.name,
        "sets": cell.sets,
        "reps": cell.reps,
        "load": cell.load,
        "load_type": cell.load_type,
        "rpe": cell.rpe,
        "rest": cell.rest,
        "note": cell.note,
        "skipped": cell.skipped,
    }
    # The designer renders a single `tag`; the model stores a list.
    if cell.tags:
        data["tag"] = cell.tags[0]
    return data


def serialize_proposed_change(change):
    """One agent-proposed edit, in the shape the review screen renders.

    Matches the prototype's ``PROPOSED_CHANGES`` dicts (``id``/``kind``/``day``/
    ``title``/``before``/``after``/``rationale``/``honors``) so the same template
    renders real batches; ``status`` is added for the (Phase 2) approve gate.
    ``member`` names the group member a per-athlete ``adjust`` targets (blank for
    every other kind, which edits the shared row) so the coach sees who diverges.
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
        "member": (
            change.membership.relationship.athlete.display_name()
            if change.membership_id
            else ""
        ),
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

    changes = [
        serialize_proposed_change(c)
        for c in batch.changes.select_related("membership__relationship__athlete")
    ]
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
    """One training day (a column in the coach designer grid).

    Returns every live cell (``session.cells()``, the P0 fixed-lineup cutover) —
    including one-week ``skipped`` exceptions, which carry a ``skipped`` flag so
    the grid can mark them (the P1 table renders an em-dash). Keeping them here
    means the row/day id-sets the designer renders match the reorder/move
    endpoints exactly. Athlete-facing surfaces use ``trainable_cells()`` instead,
    so a skipped lift is never presented as loggable.
    """
    return {
        "id": session.pk,
        "n": session.day_number,
        "name": session.name,
        "bias": session.bias,
        "exercises": [serialize_prescription(c) for c in session.cells()],
    }


def _week_label(week):
    """The week strip / grid's short label, e.g. "Wk 3"."""
    return f"Wk {week.index}"


def serialize_week(week):
    """One column in the designer's week strip.

    ``id``/``index`` let the client target a week for the switcher (view, add,
    set-current); ``current`` flags the live (deliver-target) week.
    """
    return {
        "id": week.pk,
        "index": week.index,
        "label": _week_label(week),
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
    delivery can diff against it ("changes since last delivery"). Only live
    sessions (and, via ``serialize_session``'s ``session.cells()``, live
    exercise rows) are included (soft delete, designer framework Phase 0).
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
            serialize_session(s) for s in week.sessions.filter(deleted_at__isnull=True)
        ],
    }


# Prescription fields a coach cares about when reviewing "what changed", with the
# label the deliver screen shows. ``name`` first so a swap reads as the headline
# change; ``tag`` last (it's only present when the row carries one).
_PRESCRIPTION_DIFF_FIELDS = (
    ("name", "Exercise"),
    ("sets", "Sets"),
    ("reps", "Reps"),
    ("load", "Load"),
    ("load_type", "Load type"),
    ("rpe", "RPE"),
    ("rest", "Rest"),
    ("note", "Note"),
    # A one-week skip (P2) is athlete-facing: applying/lifting it changes what she
    # trains that week, so it diffs like any other field (rendered on/off, not the
    # boolean "— → True" a |default fallback gives).
    ("skipped", "Skipped"),
    ("tag", "Tag"),
)

# Week-level meta the snapshot captures alongside the grid.
_WEEK_DIFF_FIELDS = (
    ("phase", "Phase"),
    ("volume", "Volume"),
    ("intensity", "Intensity"),
    ("is_deload", "Deload"),
)


def _prescription_label(presc):
    """A compact one-line label for an added/removed exercise row."""
    name = presc.get("name") or "Exercise"
    sets = presc.get("sets")
    reps = presc.get("reps")
    if sets and reps:
        return f"{name} {sets}×{reps}"
    return name


def _diff_fields(before, after, fields):
    """Per-field before/after for two dicts over ``fields`` (key, label) pairs."""
    out = []
    for key, label in fields:
        old = before.get(key)
        new = after.get(key)
        if old != new:
            out.append({"field": key, "label": label, "before": old, "after": new})
    return out


def _diff_exercises(current, previous):
    """Added / removed / changed exercise rows between two session grids.

    Rows match by pk (``id``) — a stable DB identity across edits — so an edited
    row reads as *changed* (with per-field before/after), a brand-new row as
    *added*, and a deleted one as *removed*. (A delete-then-re-add of the same
    movement honestly shows as remove + add, since it's a different row.)

    Exception-aware (P2 → P3): the diff is *athlete-facing*, so one-week skips
    don't manufacture phantom changes. The "add-this-week" action seeds a fresh
    ``skipped`` placeholder cell in every non-target week; on a re-delivery its
    new pk would otherwise read as an *added* exercise the athlete never trains.
    So a row absent from the other snapshot counts as added/removed only when it
    is actually trained (``skipped`` False), and a row skipped in *both*
    snapshots suppresses its field edits (trained in neither delivery). A skip
    *applied* or *lifted* (the flag flips) still surfaces — it changes her week.
    """
    prev_by_id = {e.get("id"): e for e in previous}
    cur_by_id = {e.get("id"): e for e in current}
    added = [
        {"label": _prescription_label(e)}
        for e in current
        if e.get("id") not in prev_by_id and not e.get("skipped")
    ]
    removed = [
        {"label": _prescription_label(e)}
        for e in previous
        if e.get("id") not in cur_by_id and not e.get("skipped")
    ]
    changed = []
    for e in current:
        prev_e = prev_by_id.get(e.get("id"))
        if prev_e is None:
            continue
        # Skipped in both snapshots → not trained in either delivery, so its
        # field edits aren't athlete-facing (a flip out of/into skipped is,
        # since ``skipped`` itself is one of the diffed fields below).
        if prev_e.get("skipped") and e.get("skipped"):
            continue
        fields = _diff_fields(prev_e, e, _PRESCRIPTION_DIFF_FIELDS)
        if fields:
            changed.append({"name": e.get("name") or "Exercise", "fields": fields})
    return {"added": added, "removed": removed, "changed": changed}


def _session_label(session):
    return session.get("name") or f"Day {session.get('n')}"


def diff_week_snapshots(current, previous):
    """Diff a week's live snapshot against the one last delivered.

    Both args are ``serialize_week_snapshot`` payloads. Sessions match by pk;
    a session in ``current`` but not ``previous`` is wholly *added* (and not
    double-counted as per-row diffs), the reverse is *removed*, and a session in
    both yields its added/removed/changed exercise rows. Week-meta differences
    (phase/volume/intensity/deload) are surfaced separately. Returns ``None``
    when there's nothing to diff against (no/blank prior payload); otherwise a
    dict whose ``has_changes`` is ``False`` when the week is unchanged since its
    last delivery.
    """
    if not previous or "sessions" not in previous:
        return None

    cur_sessions = current.get("sessions", [])
    prev_sessions = previous.get("sessions", [])
    prev_by_id = {s.get("id"): s for s in prev_sessions}
    cur_ids = {s.get("id") for s in cur_sessions}

    session_diffs = []
    added_sessions = []
    for s in cur_sessions:
        prev_s = prev_by_id.get(s.get("id"))
        if prev_s is None:
            added_sessions.append(
                {
                    "name": _session_label(s),
                    "day": s.get("n"),
                    "count": len(s.get("exercises", [])),
                }
            )
            continue
        ex = _diff_exercises(s.get("exercises", []), prev_s.get("exercises", []))
        if ex["added"] or ex["removed"] or ex["changed"]:
            session_diffs.append({"name": _session_label(s), "day": s.get("n"), **ex})

    removed_sessions = [
        {"name": _session_label(s), "day": s.get("n")}
        for s in prev_sessions
        if s.get("id") not in cur_ids
    ]

    week_changes = _diff_fields(
        previous.get("week", {}), current.get("week", {}), _WEEK_DIFF_FIELDS
    )

    return {
        "has_changes": bool(
            session_diffs or added_sessions or removed_sessions or week_changes
        ),
        "sessions": session_diffs,
        "added_sessions": added_sessions,
        "removed_sessions": removed_sessions,
        "week": week_changes,
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
    the week their coach just sent. Post-P3 the athlete home renders the whole
    delivered *block* (this week's ``mesocycle``) as a read-only multi-week table
    and uses this week as the block anchor + the focus-week fallback when no
    delivered week is flagged current. See ``docs/archive/meso/athlete-plan.md``.
    """
    # P3 delivers a whole block at once, so every live week of that block shares
    # one ``delivered_at`` — ordering on it alone would tie non-deterministically.
    # Break the tie toward the week the athlete is on (``is_current``), then the
    # earliest index, so the anchor/fallback is stable and meaningful. (Per-week
    # delivery gives distinct timestamps, so the tiebreak never engages there.)
    return (
        models.Week.objects.filter(
            mesocycle__plan=plan, delivered_at__isnull=False, deleted_at__isnull=True
        )
        .select_related("mesocycle")
        .order_by("-delivered_at", "-is_current", "index")
        .first()
    )


def current_week(plan, week=None):
    """The week the designer opens to — and, post-P3, the week the athlete is on.

    An explicit ``week`` wins (callers are expected to have already checked it
    is live — the delete endpoints pin the response to the just-touched row's
    own, still-live, week); otherwise the flagged current week among the
    plan's **live** weeks, or — failing both — the earliest live week in the
    plan. The athlete home focuses this week (when it's a delivered week of the
    block it renders) so "today's session" comes from where the athlete is.
    """
    if week is not None:
        return week
    weeks = list(
        models.Week.objects.filter(mesocycle__plan=plan, deleted_at__isnull=True)
        .select_related("mesocycle")
        .order_by("mesocycle__order", "index")
    )
    for candidate in weeks:
        if candidate.is_current:
            return candidate
    return weeks[0] if weeks else None


# -- per-athlete overrides: the ``adj`` overlay (groups slice S1, Phase 3) --
#
# A group plan is a *shared* program; each member can carry per-row auto-adjusts
# (a ``PrescriptionOverride`` — a swap, a load %, a volume tweak). A member's
# *effective* program = the shared template + their override diffs. The designer
# renders these as a per-row ``adj`` badge driven by the real diffs.


def resolve_load(load, load_pct, *, load_type=None):
    """Scale a numeric ``load`` by ``load_pct``, rounded to its natural step.

    An **absolute** load rounds to the nearest 2.5, mirroring the designer's
    ``round25`` so a coach-set per-athlete % lands on a loadable plate. A
    **%1RM** load (``LoadType.PERCENT``) isn't plate-constrained, so it rounds to
    the nearest 0.5% instead — 80% @ 90% is 72%, not 72.5%. A non-numeric base
    ("BW") or an absent ``load_pct`` is returned unchanged.
    """
    if load_pct is None:
        return load
    base = _num(load)
    if base is None:
        return load
    scaled = base * load_pct / 100
    # Both round a half *up* (``int(x + 0.5)``) to match the designer's
    # ``Math.round``; the scaled value is non-negative (load_pct ≥ 1), so the
    # truncating ``int`` floors to the same result. A %1RM uses a 0.5% step (no
    # plate constraint); an absolute load uses the 2.5 plate step.
    if load_type == models.LoadType.PERCENT:
        return _fmt_num(int(scaled * 2 + 0.5) / 2)
    return _fmt_num(int(scaled / 2.5 + 0.5) * 2.5)


def resolve_prescription(cell, override):
    """A member's effective row = the shared prescription cell + their override diff.

    ``cell`` is a ``Prescription`` (a fixed ``ExerciseSlot`` row × this
    ``Week``); its resolving properties already fold in any one-week swap, so
    this body is unchanged from the old per-week model. ``override`` of
    ``None`` yields the shared base unchanged. A swap replaces the name;
    ``load_pct`` scales the load; ``sets``/``reps``/``note`` override the
    shared volume/note when set. RPE is not per-athlete in this slice.
    """
    base = {
        "name": cell.name,
        "sets": cell.sets,
        "reps": cell.reps,
        "load": cell.load,
        # A load % scales the *number*; the load's meaning (abs/%1RM) is unchanged.
        "load_type": cell.load_type,
        "rpe": cell.rpe,
        "note": cell.note,
    }
    if override is None:
        return base
    return {
        "name": override.swap_name or cell.name,
        "sets": override.sets or cell.sets,
        "reps": override.reps or cell.reps,
        "load": resolve_load(cell.load, override.load_pct, load_type=cell.load_type),
        "load_type": cell.load_type,
        "rpe": cell.rpe,
        "note": override.note or cell.note,
    }


def override_adj_label(override):
    """A short badge for one member's adjust, e.g. "→ Box Squat · -10%".

    Folds the present diff parts into a compact label: a swap (``→ name``), a load
    delta (``-10%`` / ``+5%``, a no-op 100% omitted), and a volume tweak
    (``2×8``). A note-only adjust (no swap/load/volume) marks as ``note`` so it
    still surfaces. A truly empty override yields an empty string.
    """
    parts = []
    if override.swap_name:
        parts.append(f"→ {override.swap_name}")
    if override.load_pct is not None and override.load_pct != 100:
        delta = override.load_pct - 100
        parts.append(f"{'+' if delta > 0 else '-'}{abs(delta)}%")
    if override.sets or override.reps:
        parts.append(f"{override.sets or '—'}×{override.reps or '—'}")
    # A note-only adjust is still a real diff (``resolve_prescription`` applies it
    # and the model stores it), so it must not vanish from the badge — mark it
    # rather than emit an empty label that ``group_adjustments`` would skip.
    if not parts and override.note:
        parts.append("note")
    return " · ".join(parts)


def group_adjustments(plan, prescriptions):
    """Map each prescription's pk to its members' adjust badges (group plans).

    One query over the plan's overrides for the rendered prescriptions, scoped to
    the group's *active* members (an ended member's adjust drops off, matching
    ``active_member_users``). Returns ``{presc_id: {"adj", "adjusts"}}`` where
    ``adj`` is a per-row summary — one member's ``"{initials} {label}"``, or
    ``"N adjusts"`` for several — and ``adjusts`` the per-member breakdown. A row
    with no (effective) adjust is simply absent from the map.
    """
    if not prescriptions:
        return {}
    overrides = (
        models.PrescriptionOverride.objects.filter(
            prescription_id__in=[p.pk for p in prescriptions],
            membership__group=plan.group,
            membership__relationship__coach=plan.group.coach,
            membership__relationship__status=models.CoachAthlete.Status.ACTIVE,
        )
        .select_related("membership__relationship__athlete")
        .order_by("membership__relationship__athlete__name")
    )
    by_presc = defaultdict(list)
    for override in overrides:
        label = override_adj_label(override)
        if not label:
            continue
        name = override.membership.relationship.athlete.display_name()
        by_presc[override.prescription_id].append(
            {
                "id": str(override.membership.relationship.athlete_id),
                "name": name,
                "initials": initials(name),
                "label": label,
                # The raw stored diff, so the in-grid editor can pre-fill the
                # member's existing adjust (the label is display-only).
                "swap": override.swap_name,
                "load_pct": override.load_pct,
                "sets": override.sets,
                "reps": override.reps,
                "note": override.note,
            }
        )
    result = {}
    for presc_id, adjusts in by_presc.items():
        if len(adjusts) == 1:
            summary = f"{adjusts[0]['initials']} {adjusts[0]['label']}"
        else:
            summary = f"{len(adjusts)} adjusts"
        result[presc_id] = {"adj": summary, "adjusts": adjusts}
    return result


def serialize_group_identity(group):
    """The group's identity for the designer's Group mode (S1 Phase 2).

    What the designer hydrates Group mode from — the group name/focus, its active
    members (avatars + each one's contraindication flags), and the unique flags
    folded across the group. Mirrors ``presenters.group_detail`` but in the JSON
    shape the Alpine front-end reads, so Group mode renders off real rows instead
    of the prototype's hardcoded squad. Members are scoped to active links by
    ``active_member_users``.
    """
    members = group.active_member_users()
    member_data = []
    flags = set()
    for user in members:
        labels = [c.label for c in user.contraindications.all() if c.active]
        flags.update(labels)
        name = user.display_name()
        member_data.append(
            {
                "id": str(user.pk),
                "name": name,
                "initials": initials(name),
                "flags": labels,
            }
        )
    return {
        "id": group.pk,
        "name": group.name,
        "focus": group.focus or "General",
        "members": member_data,
        "member_count": len(member_data),
        "flags": sorted(flags),
    }


def serialize_athlete_identity(plan):
    """The individual plan's athlete identity for the designer left rail (Phase 5).

    Replaces the prototype's hardcoded athlete chrome ("Maya Okonkwo" + invented
    contraindications) with the *real* athlete — their name/initials, the plan's
    goal, and their active contraindications (the same global injuries the agent
    grounds on, so the coach sees the constraints while programming). A group plan
    has no single athlete (it carries ``group`` instead), so this returns None.
    """
    athlete = plan.athlete
    if athlete is None:
        return None
    name = athlete.display_name()
    return {
        "name": name,
        "initials": initials(name),
        "goal": plan.goal,
        "contraindications": [
            {"label": c.label, "text": c.text}
            for c in athlete.contraindications.all()
            if c.active
        ],
    }


def serialize_plan_history(plan):
    """The designer's undo/redo button state (Phase 1 op-log).

    ``can_undo``/``can_redo`` reflect whether the plan has a row on the
    respective stack; the labels are always the row that would pop *next* (the
    max-seq undo row / min-seq redo row — see ``history.py``), so the buttons
    can show what they're about to do. Rides every ``serialize_plan`` response
    (the initial page payload, every week/add/delete/set-current reply) so the
    designer's buttons stay accurate after every ``applyPlanData``.
    """
    undo_row = (
        models.PlanAction.objects.filter(plan=plan, stack=models.PlanAction.Stack.UNDO)
        .order_by("-seq")
        .first()
    )
    redo_row = (
        models.PlanAction.objects.filter(plan=plan, stack=models.PlanAction.Stack.REDO)
        .order_by("seq")
        .first()
    )
    return {
        "can_undo": undo_row is not None,
        "can_redo": redo_row is not None,
        "undo_label": undo_row.label if undo_row else None,
        "redo_label": redo_row.label if redo_row else None,
    }


def serialize_plan(plan, week=None):
    """Serialize ``plan`` to the designer's ``program``/``weeks``/``phases`` shape.

    ``week`` optionally pins which week populates ``program``/``weeks``;
    otherwise the flagged current week (or the plan's first) is used. A group plan
    (rooted at a ``MesoGroup``) carries a ``group`` identity payload and skips the
    athlete-scoped "last time" column; an individual plan carries ``group: None``.
    """
    open_week = current_week(plan, week)
    current_mesocycle = open_week.mesocycle if open_week else None

    if open_week is not None:
        # Soft delete (designer framework Phase 0): only live sessions surface
        # in the grid, and — matching ``serialize_session`` — only their live
        # cells (``session.cells()``, P0 fixed-lineup cutover) feed the "last
        # time" / 1RM / adjust overlays below.
        sessions = list(open_week.sessions.filter(deleted_at__isnull=True))
        program = [serialize_session(s) for s in sessions]
        prescriptions = [c for s in sessions for c in s.cells()]
        if not plan.is_group:
            # Light up the "last time" column from real logs (athlete Phase 3):
            # one query over the plan's logged sets, mapped onto the rendered
            # prescriptions. A group plan has no single athlete, so no "last".
            last_map = last_logged_labels(plan, prescriptions, plan.unit)
            # The athlete's persisted, log-derived 1RM per lift, so the coach sees
            # what a %1RM target translates to when prescribing one. Local import:
            # ``one_rm`` imports this module. A group plan has no single athlete.
            from .one_rm import one_rm_values

            one_rm_map = one_rm_values(plan.athlete, prescriptions, plan.unit)
            for session_data in program:
                for exercise in session_data["exercises"]:
                    label = last_map.get(exercise["id"])
                    if label:
                        exercise["last"] = label
                    one_rm = one_rm_map.get(exercise["id"])
                    if one_rm is not None:
                        exercise["one_rm"] = _fmt_num(one_rm.value)
                        # The coach designer distinguishes a log-derived estimate
                        # from a value the coach/athlete set, and repaints it after
                        # an edit (1RM Phase 3 — the editable %1RM badge).
                        exercise["one_rm_source"] = one_rm.source
        else:
            # A group plan instead carries the per-athlete adjust overlay (groups
            # Phase 3): a per-row ``adj`` badge driven by the members' real
            # override diffs (one query over the plan's overrides).
            adj_map = group_adjustments(plan, prescriptions)
            for session_data in program:
                for exercise in session_data["exercises"]:
                    entry = adj_map.get(exercise["id"])
                    if entry:
                        exercise["adj"] = entry["adj"]
                        exercise["adjusts"] = entry["adjusts"]
        week_strip = [
            serialize_week(w)
            for w in current_mesocycle.weeks.filter(deleted_at__isnull=True)
        ]
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
        "group": serialize_group_identity(plan.group) if plan.is_group else None,
        # The real athlete identity for an individual plan's left rail (Phase 5);
        # ``None`` for a group plan, which renders off ``group`` instead.
        "athlete": serialize_athlete_identity(plan),
        "program": program,
        "weeks": week_strip,
        # The id of the week whose grid ``program`` holds — the *viewed* week,
        # which the multi-week switcher may point away from the live (current)
        # one. The client tracks this so it can highlight the open week and tell
        # "viewing" apart from "current" (the deliver target).
        "viewing": open_week.pk if open_week is not None else None,
        "phases": phases,
        # Undo/redo button state (designer framework Phase 1) — rides every
        # serialize_plan response so the designer's buttons stay accurate.
        "history": serialize_plan_history(plan),
    }


def _pick_session_id(slot_id, sessions_by_slot, current_week_id, weeks):
    """The live ``Session`` pk the P1 grid uses for one day column.

    Prefers the current (deliver-target) week's session for this slot, since
    that's the row the write endpoints (add-exercise, remove-day) already key
    off of; falls back to the earliest live week that has one (``weeks`` is
    already ordered by ``index``) when the current week is missing a session
    for this slot (e.g. it was independently soft-deleted). ``None`` only when
    no live week has a session for this slot at all.
    """
    by_week = sessions_by_slot.get(slot_id)
    if not by_week:
        return None
    if current_week_id is not None and current_week_id in by_week:
        return by_week[current_week_id]
    for week in weeks:
        if week.pk in by_week:
            return by_week[week.pk]
    return None


def serialize_mesocycle_grid(mesocycle):
    """The P1 multi-week table: every live day × row × week cell, densely.

    Unlike ``serialize_plan``'s single-week ``program`` (one week's sessions),
    this renders the whole block at once — one row per live ``ExerciseSlot``,
    one column per live ``Week``, keyed by ``str(week_id)`` so the grid can
    look a cell up directly. A cell is live iff both its ``ExerciseSlot`` and
    its ``Week`` are live (``Prescription`` carries no ``deleted_at`` of its
    own); every live (slot × week) pair should have exactly one, built from a
    single query grouped in Python to avoid N+1 over the block.
    """
    plan = mesocycle.plan
    weeks = list(mesocycle.weeks.filter(deleted_at__isnull=True).order_by("index"))
    week_ids = [w.pk for w in weeks]
    current_week_id = next((w.pk for w in weeks if w.is_current), None)

    session_slots = list(
        mesocycle.session_slots.filter(deleted_at__isnull=True).order_by(
            "order", "day_number"
        )
    )
    slot_ids = [s.pk for s in session_slots]

    # session_id resolution: one query over every live session for this
    # block's live slots/weeks, grouped ``slot_id -> {week_id: session_id}``
    # so ``_pick_session_id`` can prefer the current week per slot.
    sessions_by_slot = defaultdict(dict)
    for sess in models.Session.objects.filter(
        week_id__in=week_ids, session_slot_id__in=slot_ids, deleted_at__isnull=True
    ):
        sessions_by_slot[sess.session_slot_id][sess.week_id] = sess.pk

    exercise_slots = list(
        models.ExerciseSlot.objects.filter(
            session_slot_id__in=slot_ids, deleted_at__isnull=True
        ).order_by("order")
    )
    exercise_slot_ids = [e.pk for e in exercise_slots]
    rows_by_slot = defaultdict(list)
    for exercise_slot in exercise_slots:
        rows_by_slot[exercise_slot.session_slot_id].append(exercise_slot)

    # One query for every live cell in the block, grouped by (slot, week) so
    # each row's dense ``cells`` map is built without a per-row lookup.
    # ``select_related("swap_exercise")`` lets swap_display resolve a
    # catalog-only swap's name below without a per-cell query.
    cells_by_key = {
        (cell.exercise_slot_id, cell.week_id): cell
        for cell in models.Prescription.objects.filter(
            exercise_slot_id__in=exercise_slot_ids, week_id__in=week_ids
        ).select_related("swap_exercise")
    }

    days = []
    for slot in session_slots:
        rows = []
        for exercise_slot in rows_by_slot.get(slot.pk, []):
            cells = {}
            for week in weeks:
                cell = cells_by_key.get((exercise_slot.pk, week.pk))
                if cell is None:
                    continue
                # The resolved one-week swap display name: free-text
                # swap_name if set, else the swapped catalog exercise's name,
                # else "" (no swap) — the table needs this to show a badge
                # for catalog-only swaps too (swap_name blank).
                if cell.swap_name:
                    swap_display = cell.swap_name
                elif cell.swap_exercise_id:
                    swap_display = cell.swap_exercise.name
                else:
                    swap_display = ""
                cells[str(week.pk)] = {
                    "prescription_id": cell.pk,
                    "sets": cell.sets,
                    "reps": cell.reps,
                    "load": cell.load,
                    "load_type": cell.load_type,
                    "rpe": cell.rpe,
                    "rest": cell.rest,
                    "note": cell.note,
                    "skipped": cell.skipped,
                    "swap_name": cell.swap_name,
                    "swap_exercise_id": cell.swap_exercise_id,
                    "swap_display": swap_display,
                }
            rows.append(
                {
                    "exercise_slot_id": exercise_slot.pk,
                    "name": exercise_slot.name,
                    "exercise_id": exercise_slot.exercise_id,
                    "order": exercise_slot.order,
                    "tags": list(exercise_slot.tags or []),
                    "cells": cells,
                }
            )
        days.append(
            {
                "session_slot_id": slot.pk,
                "session_id": _pick_session_id(
                    slot.pk, sessions_by_slot, current_week_id, weeks
                ),
                "day_number": slot.day_number,
                "name": slot.name,
                "bias": slot.bias,
                "order": slot.order,
                "rows": rows,
            }
        )

    return {
        "mesocycle": {
            "id": mesocycle.pk,
            "plan_id": plan.pk,
            "name": mesocycle.name,
            "week_count": mesocycle.week_count,
        },
        "weeks": [
            {
                "id": w.pk,
                "index": w.index,
                "label": _week_label(w),
                "phase": w.phase,
                "deload": w.is_deload,
                "current": w.is_current,
                "delivered_at": w.delivered_at.isoformat() if w.delivered_at else None,
            }
            for w in weeks
        ],
        "days": days,
        "history": serialize_plan_history(plan),
    }
