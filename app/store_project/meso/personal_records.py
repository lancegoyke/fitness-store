"""Personal records — derive-on-read best e1RM *with provenance*, and new-PR detection.

Parity plan §6 ("Later — Extensions" → personal records). The athlete's estimated
1RM per lift already exists (``AthleteOneRm`` + ``one_rm.derive_one_rm_values``);
this slice adds the two things that make a *record* rather than a bare number:

- **provenance** — which logged set, on which date, achieved the best;
- **new-PR detection** — did a just-logged session beat the athlete's prior best.

Both ride on the same structured performed record, ``LoggedSet`` — never parsed
free text — and reuse the pinned Epley math verbatim (``one_rm.epley_one_rm``) and
the hybrid B4 lift identity (``serializers._exercise_key`` via
``one_rm.key_str``). The scan is DONE-only and unit-scoped exactly as
``derive_one_rm_values`` (a bare logged load is denominated in its plan's unit, so
kg and lb sets for one lift must never pool).

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

from . import models
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

    key: str
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


def _completed_logged_sets(athlete, *, unit):
    """The athlete's DONE, prescription-linked logged sets, scoped to ``unit``.

    Mirrors ``one_rm.derive_one_rm_values``'s query: a pending "Save progress"
    draft is not a finished performance, and a bare logged load is denominated in
    its plan's unit — pooling kg and lb sets for one lift would be unit-confused.
    """
    return models.LoggedSet.objects.filter(
        session_log__athlete=athlete,
        session_log__status=models.SessionLog.Status.DONE,
        session_log__session__week__mesocycle__plan__unit=unit,
        prescription__isnull=False,
    ).select_related("prescription__exercise_slot", "session_log")


def _performed_sets(logged_sets, *, unit):
    """Normalize a ``LoggedSet`` iterable into :class:`_PerformedSet` tuples.

    The bridge from the stored record to the seam: identity via
    ``one_rm.key_str`` (``serializers._exercise_key``), estimate via
    ``one_rm.epley_one_rm``. A set whose load/reps aren't a usable number ("BW",
    "AMRAP", "") yields ``None`` from Epley and is dropped (never a crash).
    """
    for ls in logged_sets:
        est = epley_one_rm(ls.load, ls.reps)
        if est is None:
            continue
        yield _PerformedSet(
            key=key_str(ls.prescription.exercise_id, ls.prescription.name),
            name=ls.prescription.name,
            unit=unit,
            reps=ls.reps,
            load=ls.load,
            e1rm=est,
            logged_set_id=ls.id,
            session_log_id=ls.session_log_id,
            date=ls.session_log.date,
        )


def _best_per_lift(performed):
    """Best (max e1RM) :class:`PersonalRecord` per lift identity — the seam.

    Consumes any iterable of :class:`_PerformedSet`; ties keep the first-seen set
    (a strict ``>`` never displaces an equal earlier best). This is the whole
    computation both public functions share.
    """
    best: dict[str, PersonalRecord] = {}
    for ps in performed:
        current = best.get(ps.key)
        if current is None or ps.e1rm > current.e1rm:
            best[ps.key] = PersonalRecord(
                key=ps.key,
                name=ps.name,
                unit=ps.unit,
                e1rm=ps.e1rm,
                reps=ps.reps,
                load=ps.load,
                date=ps.date,
                logged_set_id=ps.logged_set_id,
                session_log_id=ps.session_log_id,
            )
    return best


def _session_unit(session_log):
    """The unit a session's logged loads are denominated in (its plan's)."""
    return session_log.session.week.mesocycle.plan.unit


def personal_records(athlete, *, unit):
    """Best e1RM per lift for ``athlete`` in ``unit``, keyed by B4 identity.

    Derive-on-read (nothing persisted): ``{key: PersonalRecord}`` over the
    athlete's DONE, same-unit logged sets, each record carrying the display name,
    best Epley e1RM (the raw ``epley_one_rm`` value), the winning reps/load
    strings, the date, and the source ``LoggedSet``/``SessionLog`` ids. A lift
    with no usable set is absent.
    """
    logged_sets = _completed_logged_sets(athlete, unit=unit)
    return _best_per_lift(_performed_sets(logged_sets, unit=unit))


def new_records_in(session_log):
    """Lifts in ``session_log`` that beat the athlete's prior best — pure detection.

    No side effects (no writes, no ``PlanAction``): returns a list of
    :class:`NewRecord`, one per lift in this DONE session whose best e1RM exceeds
    the athlete's best over *all other* same-unit DONE sets. The comparison
    excludes the session under test, so a lone first-ever log is a PR (``previous``
    is ``None``) rather than a tie against itself; a tie or a lighter session is
    not a PR. A pending session (not a finished performance) yields nothing.
    """
    if session_log.status != models.SessionLog.Status.DONE:
        return []

    unit = _session_unit(session_log)
    this_sets = models.LoggedSet.objects.filter(
        session_log=session_log, prescription__isnull=False
    ).select_related("prescription__exercise_slot", "session_log")
    this_best = _best_per_lift(_performed_sets(this_sets, unit=unit))
    if not this_best:
        return []

    # Prior best over the athlete's OTHER same-unit DONE sets — excluding this
    # session, so the session can't be its own prior record.
    prior_qs = _completed_logged_sets(session_log.athlete, unit=unit).exclude(
        session_log=session_log
    )
    prior_best = _best_per_lift(_performed_sets(prior_qs, unit=unit))

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
                )
            )
    return records
