"""Personal records — derive-on-read best e1RM *with provenance*, and new-PR detection.

Parity plan §6 ("Later — Extensions" → personal records). The athlete's estimated
1RM per lift already exists (``AthleteOneRm`` + ``one_rm.derive_one_rm_values``);
this slice adds the two things that make a *record* rather than a bare number:

- **provenance** — which logged set, on which date, achieved the best;
- **new-PR detection** — did a just-logged session beat the athlete's prior best.

Both ride on the same structured performed record, ``LoggedSet`` — never parsed
free text — and reuse the pinned Epley math verbatim (``one_rm.epley_one_rm``) and
the lift identity in ``lift_identity.py`` (#708): a set counts toward the lift
it was STAMPED with at write time (``LoggedSet.lift``), not whatever its slot
is called now, so a swap or rename of the slot doesn't relabel past records.
The scan is unit-scoped exactly as ``derive_one_rm_values`` (a bare logged
load is denominated in its plan's unit, so kg and lb sets for one lift must
never pool).

**LIVE, not DONE-only (5a, plan §7).** There is no "I'm done" button anymore —
completion dissolved into a 24 h quiet-period settle (5b). So unlike
``one_rm.derive_one_rm_values`` (which stays DONE-only — it writes the
*persisted*, confirmed ``AthleteOneRm``, see that module's docstring), this
module's reads count **PENDING** sets too: a "best so far" that's live and
self-healing — edit/correct a cell and the next read re-derives off the
corrected text, no separate reconciliation step. The trade is an occasional
false-positive PR (a set later corrected downward) in exchange for
in-the-moment feedback; 5b's settle sweep is what eventually promotes a live
best into the confirmed, persisted record.

Nothing is persisted (no ``PersonalRecord`` table — that is a deliberate later
slice) and ``new_records_in`` has no side effects (no writes, no ``PlanAction``).

**Seam.** The best-per-lift computation consumes an *iterable of normalized
performed sets* (:class:`_PerformedSet` tuples: key, name, unit, reps, load,
e1rm, provenance) via :func:`_best_per_lift`; the ``LoggedSet`` query only feeds
that helper (:func:`_performed_sets`). A future free-text/parsed performance feed
can drive the SAME computation by yielding the same tuples — no rework here.
"""

from dataclasses import dataclass
from datetime import date as date_cls

from django.db.models import Q

from . import models
from .lift_identity import Lift
from .lift_identity import LiftIndex
from .lift_identity import representatives
from .one_rm import epley_one_rm
from .one_rm import key_str


@dataclass(frozen=True)
class _PerformedSet:
    """One normalized performed set — the seam's input tuple.

    Unit-agnostic to its source: a ``LoggedSet`` produces it today, a parsed
    free-text feed could produce it tomorrow, and :func:`_best_per_lift` treats
    them identically. ``e1rm`` is whatever :func:`one_rm.epley_one_rm` returned
    (never re-derived here), so the winning set's estimate is exactly the pinned
    Epley value the client and server agree on.
    """

    lift: Lift
    name: str
    unit: str
    reps: str
    load: str
    e1rm: float
    logged_set_id: int
    session_log_id: int
    date: date_cls | None


@dataclass(frozen=True)
class PersonalRecord:
    """The best e1RM for one lift, with the provenance that produced it."""

    key: str
    name: str
    unit: str
    e1rm: float
    reps: str
    load: str
    date: date_cls | None
    logged_set_id: int
    session_log_id: int
    lift: Lift | None = None
    # The winning set's own stamp; differs from ``lift`` (the record's
    # representative) when the set was folded in by name.
    performed_lift: Lift | None = None


@dataclass(frozen=True)
class NewRecord:
    """A lift in a just-logged session that beat the athlete's prior best."""

    key: str
    name: str
    unit: str
    value: float
    previous: float | None
    reps: str
    load: str
    logged_set_id: int
    lift: Lift | None = None
    performed_lift: Lift | None = None


def _live_logged_sets(athlete, *, unit):
    """The athlete's LIVE, anchor-linked logged sets, scoped to ``unit``.

    Unlike ``one_rm.derive_one_rm_values``'s query (which this used to mirror
    exactly, DONE-only), this one is deliberately **not** status-filtered
    (5a, plan §7): a PENDING (typed, not yet finished) draft counts
    toward the *live* best, because an athlete need never tap "Finish
    session" — a 24 h settle (5b) later promotes a live best into the
    persisted, confirmed ``AthleteOneRm``. A bare logged load is still
    denominated in its plan's unit, so kg and lb sets for one lift must never
    pool — that scoping is unchanged.

    ``.anchored()`` (#578 C1), not ``prescription__isnull=False``: admits a
    set whose ``prescription`` went NULL (a hard-deleted line-0 cell,
    #577/#581) but whose ``exercise_slot`` survives, so a stray hard delete
    no longer silently detaches an otherwise-live set from this scan.

    Reads ``LoggedSet.objects.performance_history`` (#575): only the newest log
    of each ``(session, athlete)`` pair counts, whatever its status, and a
    set on a soft-deleted day still does.
    """
    return (
        models.LoggedSet.objects.performance_history(athlete)
        .filter(session_log__session__week__mesocycle__plan__unit=unit)
        .anchored()
        .select_related("session_log")
    )


def _performed_sets(logged_sets, *, unit):
    """Normalize a ``LoggedSet`` iterable into :class:`_PerformedSet` tuples.

    The bridge from the stored record to the seam: identity via
    ``one_rm.key_str`` (``serializers._exercise_key``), estimate via
    ``one_rm.epley_one_rm``. A set whose load/reps aren't a usable number ("BW",
    "AMRAP", "") yields ``None`` from Epley and is dropped (never a crash).

    Identity is ``ls.lift`` (#708): the write-time stamp, falling back to the
    anchor slot's live identity for an unstamped row.
    """
    for ls in logged_sets:
        est = epley_one_rm(ls.load, ls.reps)
        if est is None:
            continue
        lift = ls.lift
        if lift is None:
            continue
        yield _PerformedSet(
            lift=lift,
            name=lift.name,
            unit=unit,
            reps=ls.reps,
            load=ls.load,
            e1rm=est,
            logged_set_id=ls.id,
            session_log_id=ls.session_log_id,
            date=ls.session_log.date,
        )


def _best_per_lift(performed, targets=None):
    """Best (max e1RM) :class:`PersonalRecord` per lift — the seam.

    Consumes any iterable of :class:`_PerformedSet`; ties keep the first-seen set
    (a strict ``>`` never displaces an equal earlier best). ``targets`` are the
    lifts to report; by default one per ``representatives`` lift of the history.
    Each target's best is taken over every set matching it under
    ``lift_identity.same_lift`` and is keyed by ``key_str`` of the target. This is
    the whole computation both public functions share.
    """
    performed = list(performed)
    if targets is None:
        targets = representatives(ps.lift for ps in performed)
    index = LiftIndex(performed, lift=lambda ps: ps.lift)
    best: dict[str, PersonalRecord] = {}
    for target in targets:
        key = key_str(target.exercise_id, target.name)
        for ps in index.matching(target):
            current = best.get(key)
            if current is None or ps.e1rm > current.e1rm:
                best[key] = PersonalRecord(
                    key=key,
                    # The winning set's own name, as the athlete logged it:
                    # deterministic, unlike "first name seen" for an FK known
                    # under several names.
                    name=ps.name,
                    unit=ps.unit,
                    e1rm=ps.e1rm,
                    reps=ps.reps,
                    load=ps.load,
                    date=ps.date,
                    logged_set_id=ps.logged_set_id,
                    session_log_id=ps.session_log_id,
                    lift=target,
                    performed_lift=ps.lift,
                )
    return best


def _session_unit(session_log):
    """The unit a session's logged loads are denominated in (its plan's)."""
    return session_log.session.week.mesocycle.plan.unit


def personal_records(athlete, *, unit):
    """Best LIVE e1RM per lift for ``athlete`` in ``unit``, keyed by B4 identity.

    Derive-on-read (nothing persisted): ``{key: PersonalRecord}`` over the
    athlete's same-unit logged sets — PENDING included (5a, plan §7; see the
    module docstring) — each record carrying the display name, best Epley
    e1RM (the raw ``epley_one_rm`` value), the winning reps/load strings, the
    date, and the source ``LoggedSet``/``SessionLog`` ids. A lift with no
    usable set is absent. Self-healing: correcting the set that produced a
    best simply changes what the next call returns, no invalidation needed.
    """
    logged_sets = _live_logged_sets(athlete, unit=unit)
    return _best_per_lift(_performed_sets(logged_sets, unit=unit))


def _logged_before(session_log):
    """Sets from logs that PRECEDE ``session_log``, by the model's own order.

    Restricting a settled subject to settled history was only half the rule.
    ``session_results`` recomputes this on every read, so without a chronology
    a session that settles LATER also rewrote the earlier verdict: a coach
    opening a finished session saw its PR badge simply gone, because the athlete
    has since out-lifted it. Whether that session was a record is a fact about
    the day it happened, and a later one cannot change it.

    Ordered like ``SessionLog.Meta`` — ``date`` first, ``created_at`` as the
    tiebreak — with an undated log falling back to when it was written, since
    ``date__lt`` would otherwise drop it from the baseline entirely.
    """
    when = session_log.date
    if when is None:
        return Q(session_log__created_at__lt=session_log.created_at)
    return (
        Q(session_log__date__lt=when)
        | Q(
            session_log__date=when,
            session_log__created_at__lt=session_log.created_at,
        )
        | Q(
            session_log__date__isnull=True,
            session_log__created_at__lt=session_log.created_at,
        )
    )


def new_records_in(session_log):
    """Lifts in ``session_log`` that beat the athlete's prior LIVE best — pure detection.

    No side effects (no writes, no ``PlanAction``): returns a list of
    :class:`NewRecord`, one per lift in this session whose best e1RM exceeds the
    athlete's best over *all other* same-unit logged sets. The comparison
    excludes the session under test, so a lone first-ever log is a PR (``previous``
    is ``None``) rather than a tie against itself; a tie or a lighter session is
    not a PR.

    **PENDING counts too (5a, plan §7)** — there is no "I'm done" button
    anymore, and this is what powers the *optimistic* toast fired straight off
    a grid-cell blur (``athlete_cell_write``), before the session ever settles
    to DONE. That's an accepted trade: an optimistic PR can occasionally be a
    false alarm if the set is later corrected downward — the *confirmed*
    celebration (5b) is what settles it after a 24 h quiet period. A session
    with no usable set yields nothing.
    """
    unit = _session_unit(session_log)
    # `.anchored()` (#578 C1), not `prescription__isnull=False` — see
    # `_live_logged_sets`, which this mirrors so `_performed_sets` can rely on
    # `anchor_slot` resolving for every row from either query.
    this_sets = (
        models.LoggedSet.objects.filter(session_log=session_log)
        .anchored()
        .select_related("session_log")
    )
    this_performed = list(_performed_sets(this_sets, unit=unit))
    this_best = _best_per_lift(this_performed)
    if not this_best:
        return []

    # Prior best over the athlete's OTHER same-unit logged sets — excluding
    # this session, so the session can't be its own prior record.
    prior_qs = _live_logged_sets(session_log.athlete, unit=unit).exclude(
        session_log=session_log
    )
    # Compare like with like. A DONE subject is a settled performance — it is
    # what the coach's `session_results` judges — so it must be measured against
    # settled history only. Without this, a PENDING draft saved later would
    # retroactively decide whether an already-completed session was a PR: log a
    # done 120x5, later draft a pending 150x5, and the coach's view of the
    # finished session silently stops showing its record. A PENDING subject is
    # the live/optimistic path and keeps the live baseline.
    if session_log.status == models.SessionLog.Status.DONE:
        prior_qs = prior_qs.filter(
            session_log__status=models.SessionLog.Status.DONE
        ).filter(_logged_before(session_log))
    # The baseline is matched by the same targets, plus every catalog lift this
    # session carries under each of its names here: an FK target is matched
    # under the names its own index knows (``LiftIndex.matching``), and a name
    # it carries only in THIS session would otherwise be missing from the
    # prior index, under-counting the baseline into a false PR. Same key, so
    # ``_best_per_lift`` keeps the larger best.
    prior_best = _best_per_lift(
        _performed_sets(prior_qs, unit=unit),
        targets=[
            *(r.lift for r in this_best.values()),
            *(ps.lift for ps in this_performed if ps.lift.exercise_id is not None),
        ],
    )

    records = []
    for key, record in this_best.items():
        previous = prior_best.get(key)
        previous_value = previous.e1rm if previous is not None else None
        if previous_value is None or record.e1rm > previous_value:
            records.append(
                NewRecord(
                    key=key,
                    name=record.name,
                    unit=record.unit,
                    value=record.e1rm,
                    previous=previous_value,
                    reps=record.reps,
                    load=record.load,
                    logged_set_id=record.logged_set_id,
                    lift=record.lift,
                    performed_lift=record.performed_lift,
                )
            )
    return records
