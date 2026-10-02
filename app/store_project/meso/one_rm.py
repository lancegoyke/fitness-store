"""Derive, persist, and read the athlete's estimated 1RM (S2 follow-up).

A %1RM target ("75%") is an *intensity*; turning it into a bar load needs the
athlete's one-rep max. This module is the server-side home of that estimate —
promoted out of per-device localStorage (Phase 2b) into ``AthleteOneRm`` rows:

- ``epley_one_rm`` — the per-set estimate (mirrors ``meso_athlete.js``'s
  ``epleyOneRm`` exactly, so the client and server agree);
- ``derive_one_rm_values`` — the best (max) estimate per lift across the
  athlete's *completed* logged sets;
- ``refresh_one_rms`` — recompute + upsert the rows for the lifts in a session,
  called after a log save so the estimate tracks what the athlete actually did;
- ``one_rm_values`` — read the stored estimate for a batch of prescriptions
  (one query), for the athlete logger's suggested load and the coach designer.

Identity follows the hybrid B4 rule (``serializers._exercise_key``): a
catalog-linked lift by FK, a free-text lift by normalized name. See
``docs/archive/meso/one-rm-plan.md``.

**DONE-only, deliberately, even after 5a.** ``derive_one_rm_values`` /
``refresh_one_rms`` write and read the *persisted* ``AthleteOneRm`` — the
confirmed/settled estimate the coach designer and the logger's suggested-load
default are built on. ``personal_records.py`` (5a, docs/meso/
parse-at-commit-plan.md §7) deliberately went the other way for its **live**
reads: it counts PENDING parse-at-commit sets too, so the records panel/PR
toast are live and self-healing. The two modules now intentionally disagree —
this one stays DONE-only because a pending draft (lines typed, session not finished) is not a
finished performance to permanently write down; 5b's 24 h quiet-period settle
is what eventually promotes a live best into this module's confirmed record.
"""

import logging
import math
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import models
from .lift_identity import Lift
from .lift_identity import LiftIndex
from .lift_identity import lift_of
from .lift_identity import norm_name
from .lift_identity import representatives
from .serializers import _exercise_key
from .serializers import _num


def key_str(exercise_id, name):
    """The denormalized identity stored on ``AthleteOneRm.key``.

    ``"id:<pk>"`` for a catalog-linked lift, ``"name:<lower>"`` for free text —
    a string form of ``serializers._exercise_key`` so one ``unique(athlete,
    key)`` constraint spans both halves of the B4 hybrid.
    """
    kind, ident = _exercise_key(exercise_id, name)
    return f"{kind}:{ident}"


def epley_one_rm(load, reps):
    """Estimated 1RM from one logged set via Epley: ``w × (1 + reps/30)``.

    A single rep *is* a 1RM, so it returns the load unchanged (not the formula's
    slight overshoot). ``None`` when either cell isn't a usable number (load > 0,
    reps ≥ 1) — the free-text loads/reps the grid allows ("BW", "AMRAP", "8-10").
    Mirrors ``meso_athlete.js``'s ``epleyOneRm`` so client and server agree.
    """
    w = _num(load)
    r = _num(reps)
    if w is None or r is None or w <= 0 or r < 1:
        return None
    if r == 1:
        return w
    return w * (1 + r / 30)


def derive_one_rm_values(athlete, *, lifts=None, unit=None):
    """Best Epley 1RM per target lift from the athlete's *completed* logged sets.

    One query over the athlete's ``DONE`` logged sets (a pending
    draft is not a finished performance — the results/"last" surfaces treat it the
    same). Returns ``{key: float}`` keyed by ``key_str`` of each TARGET lift: the
    maximum implied 1RM over every set whose stamped ``LoggedSet.lift`` matches
    that target under ``lift_identity.same_lift`` (#708). ``lifts``, when given,
    are the targets (the lifts just logged or refreshed); untargeted, there is one
    entry per ``representatives`` lift of the sets with a usable estimate. A target
    with no matching usable set is absent. If two targets share a key the larger
    value is kept.

    Deliberately DONE-only even though ``personal_records.personal_records``
    (5a) counts PENDING sets for its *live* reads — see this module's
    docstring for why the two are allowed to disagree: this one feeds the
    persisted, confirmed ``AthleteOneRm``, not a live/self-healing panel.

    ``unit`` scopes the scan to logged sets from plans in that unit — a logged
    ``load`` is a bare number whose unit is the plan's, so pooling kg and lb sets
    for one lift would be unit-confused. The estimate is therefore derived (and
    stored) per unit.
    """
    # #578 C1: `.anchored()` (not `prescription__isnull=False`) admits a set
    # whose `prescription` went NULL (a hard-deleted line-0 cell, #577/#581)
    # but whose `exercise_slot` survives — the same identity, resolved
    # through the durable pointer instead of the one that can go stale.
    # #575: the athlete's logged sets are `LoggedSet.objects.performance_history`
    # (newest log per pair, deleted days included); DONE is this read's own filter.
    logged_sets = (
        models.LoggedSet.objects.performance_history(athlete)
        .filter(session_log__status=models.SessionLog.Status.DONE)
        .anchored()
    )
    if unit is not None:
        logged_sets = logged_sets.filter(
            session_log__session__week__mesocycle__plan__unit=unit
        )
    estimates = []
    # Every name each catalog lift carries for this athlete (#708): its stamps
    # here (usable estimate or not) and, below, the athlete's live rows linked
    # to it. An FK target is matched under exactly these, never under the
    # target's own name alone, so ``id:<pk>`` holds one value whichever row —
    # or a deleted set's leftover stamp — asked for the refresh.
    fk_names = defaultdict(set)
    for ls in logged_sets:
        lift = ls.lift
        if lift is None:
            continue
        if lift.exercise_id is not None:
            fk_names[lift.exercise_id].add(norm_name(lift.name))
        est = epley_one_rm(ls.load, ls.reps)
        if est is None:
            continue
        estimates.append((lift, est))
    if lifts is None:
        targets = representatives(lift for lift, _ in estimates)
    else:
        targets = [lift_of(x) for x in lifts]
    fks = {t.exercise_id for t in targets if t.exercise_id is not None}
    if fks:
        live_rows = models.ExerciseSlot.objects.filter(
            exercise_id__in=fks,
            deleted_at__isnull=True,
            session_slot__mesocycle__plan__relationship__athlete=athlete,
        ).values_list("exercise_id", "name")
        for exercise_id, name in live_rows:
            fk_names[exercise_id].add(norm_name(name))
    index = LiftIndex(estimates, lift=lambda e: e[0])
    best = {}
    for target in targets:
        matched = index.matching(
            target,
            names=fk_names.get(target.exercise_id, set())
            if target.exercise_id is not None
            else None,
        )
        if not matched:
            continue
        value = max(est for _, est in matched)
        key = key_str(target.exercise_id, target.name)
        if key not in best or value > best[key]:
            best[key] = value
    return best


# The largest value the ``value`` column (``Decimal(7, 2)``) can hold. A derived
# estimate beyond this is a fat-fingered logged load, not a real 1RM — skip it
# rather than let a ``DecimalField`` overflow roll back the athlete's whole log
# (refresh runs inside the log-save transaction).
_MAX_VALUE = Decimal("99999.99")


def _quantize(value):
    """A derived float as a 2-decimal ``Decimal`` for the ``value`` column."""
    return Decimal(str(round(float(value), 2)))


def refresh_one_rms(athlete, lifts, unit):
    """Recompute + persist ``athlete``'s 1RM for the lifts in ``lifts``.

    Called after a log save: for each lift identity among ``lifts``, upsert
    the ``AthleteOneRm`` row to the freshly derived best Epley estimate over
    *all* the athlete's completed logs for that lift (not just this session —
    the 1RM is a property of the athlete, not one plan). A lift with no usable
    logged set yet (no numeric load/reps anywhere) is left untouched rather than
    written as null. ``unit`` records what the stored value is denominated in.

    ``lifts`` is anything carrying the B4 identity pair (``exercise_id``,
    ``name``) — a ``Prescription`` cell or an ``ExerciseSlot`` (#578 C1:
    ``settle.settle_log`` passes the latter, resolved off ``LoggedSet.
    anchor_slot`` rather than a cell that may no longer exist). This function
    only ever reads those two attributes off each element, never anything
    cell-specific, so it doesn't care which.

    **The ``AthleteOneRm.key`` choice (#708).** The key stays the FK-first
    ``key_str`` of the TARGET lift being refreshed — unchanged format, so
    ``unique(athlete, key)`` and ``one_rm_values``' per-prescription lookup are
    untouched, and the client's ``epleyOneRm`` only mirrors the formula, never a
    key. The value is derived from every set that matches that target under
    ``lift_identity.same_lift``. So free-text "Back Squat" history folds into
    ``id:<pk>`` the first time the linked lift refreshes, while the old
    ``name:back squat`` row stays a correct answer for any free-text Back Squat
    target.

    Because a set counts toward EVERY target it matches, a change to one can
    move a stored row nobody named in ``lifts`` — a free-text Back Squat set
    feeds the ``id:<pk>`` row of a catalog Back Squat on another block. So the
    athlete's other stored LOGGED rows in ``unit`` are re-derived too, and each
    is written (or cleared) only when its value no longer matches what the logs
    support.
    """
    lifts = list(lifts)
    # One representative (exercise_id, name) per identity — a later lift's
    # name wins for display, harmless since they share the identity.
    reps_by_key = {}
    for p in lifts:
        reps_by_key[key_str(p.exercise_id, p.name)] = (p.exercise_id, p.name)
    if not reps_by_key:
        return
    # The other stored rows (#708, see the docstring). A row whose key no
    # longer agrees with its own (exercise_id, name) — its catalog exercise was
    # deleted out from under it — isn't a target any lookup can reach, so it
    # is left alone rather than re-derived under a different key.
    others = {
        row.key: row
        for row in models.AthleteOneRm.objects.filter(
            athlete=athlete, unit=unit, source=models.AthleteOneRm.Source.LOGGED
        ).exclude(key__in=reps_by_key)
        if row.key == key_str(row.exercise_id, row.name)
    }
    # A manually-entered estimate (Phase 2) is the athlete's own number — logs
    # never touch it (before, logs only ever *raised* the derived value). Skip
    # those lifts entirely so neither the upsert nor the stale-clear below runs.
    # Scoped to ``unit``: a manual row records one unit (the single row is
    # last-unit-wins, cross-unit conversion deferred), so a manual kg value must
    # not block a lb log from producing its own lb estimate — only a *same-unit*
    # manual row is protected here.
    manual_keys = set(
        models.AthleteOneRm.objects.filter(
            athlete=athlete,
            key__in=reps_by_key,
            unit=unit,
            source=models.AthleteOneRm.Source.MANUAL,
        ).values_list("key", flat=True)
    )
    # Derive from same-unit logs only, so the stored value is unambiguously in
    # ``unit`` (the unit it's written with).
    derived = derive_one_rm_values(
        athlete,
        lifts=[*lifts, *(Lift(row.exercise_id, row.name) for row in others.values())],
        unit=unit,
    )
    targets = {
        **{key: (row.exercise_id, row.name) for key, row in others.items()},
        **reps_by_key,
    }
    # Sorted, so every caller locks this athlete's rows in ONE order.
    # `update_or_create` holds each row it touches until the caller's
    # transaction ends, and two concurrent refreshes over the same lifts in
    # opposite session order — the 5b settle sweep finishing one session while
    # the athlete saves another — would otherwise deadlock on Postgres.
    for key, (exercise_id, name) in sorted(targets.items()):
        if key in manual_keys:
            continue
        value = derived.get(key)
        quantized = _quantize(value) if value is not None else None
        stored = others.get(key)
        if stored is not None:
            # Another stored row (#708). Read without a lock above, so write it
            # only if it is still the LOGGED value read then: a coach's manual
            # estimate (or another refresh) that landed since must win, never
            # be overwritten back to a logged one.
            if quantized is not None and quantized == stored.value:
                continue
            still_ours = models.AthleteOneRm.objects.filter(
                pk=stored.pk,
                source=models.AthleteOneRm.Source.LOGGED,
                value=stored.value,
            )
            if quantized is None or not (Decimal("0") < quantized <= _MAX_VALUE):
                still_ours.delete()
            else:
                # ``update()`` skips ``auto_now``; keep ``updated_at`` honest.
                still_ours.update(value=quantized, updated_at=timezone.now())
            continue
        if quantized is None or not (Decimal("0") < quantized <= _MAX_VALUE):
            # No usable same-unit estimate remains (the set was blanked / made
            # free-text, or the value won't fit the column): clear any stale row in
            # *this* unit so the logger/designer stop showing an estimate the logs
            # no longer support. A row in the other unit stays — it's derived from
            # that unit's own logs, untouched here.
            models.AthleteOneRm.objects.filter(
                athlete=athlete, key=key, unit=unit
            ).delete()
            continue
        models.AthleteOneRm.objects.update_or_create(
            athlete=athlete,
            key=key,
            defaults={
                "exercise_id": exercise_id,
                "name": name,
                "value": quantized,
                "unit": unit,
                "source": models.AthleteOneRm.Source.LOGGED,
            },
        )


logger = logging.getLogger(__name__)


def refresh_after_identity_change(plan, slots):
    """Refresh the plan athlete's stored 1RM for rows whose identity just changed (#715).

    A rename, swap, catalog link or an undo/redo of one gives the row a key no
    ``AthleteOneRm`` row exists for yet, so the athlete's %1RM suggestion went
    blank until their next finished session even though ``same_lift`` folds
    their history into the new identity. Call it inside the write's
    transaction: the refresh is queued ``on_commit``, so it never runs under the
    plan lock and ``update_or_create`` takes its ``AthleteOneRm`` rows after
    the documented plan-before-athlete-rows order has been released (docs/meso/
    decisions.md, Row-lock order). ``robust=True``: a failed refresh must not
    turn the coach's already-committed edit into a 500; the next log re-derives.

    A template plan has no athlete: nothing to refresh. A manual 1RM stays the
    athlete's own number (``refresh_one_rms`` skips it).
    """
    athlete = plan.athlete
    lifts = [lift_of(slot) for slot in slots]
    if athlete is None or not lifts:
        return
    unit = plan.unit

    def _refresh():
        try:
            refresh_one_rms(athlete, lifts, unit)
        except Exception:
            logger.exception("1RM refresh after an identity change failed")

    transaction.on_commit(_refresh, robust=True)


def lifts_for_sets(logged_sets):
    """The lifts a refresh must cover after ``logged_sets`` changed (#708).

    Each set's stamped ``lift`` (what it counts toward) AND its anchor slot's
    current identity (what the plan's cells — and so the logger's suggested
    load — look up). They differ after a swap, rename or catalog link; refreshing
    only the stamp would leave a just-linked row's ``id:`` estimate stale.
    """
    lifts = []
    for ls in logged_sets:
        if ls.lift is not None:
            lifts.append(ls.lift)
        slot = ls.anchor_slot
        if slot is not None:
            lifts.append(lift_of(slot))
    return lifts


def one_rm_values(athlete, prescriptions, unit):
    """Map each prescription pk to ``athlete``'s stored ``AthleteOneRm``, if any.

    One query over the athlete's stored estimates for the rendered lifts (by
    identity, so the same 1RM surfaces against every prescription of that lift).
    Scoped to ``unit`` (the reading plan's): the stored value is a bare number in
    its *own* unit, so surfacing it under a different unit would be wrong — a row
    in the other unit is simply omitted (the athlete will re-derive one by logging
    in this unit). A lift the athlete has no estimate for is absent from the map.
    """
    keys = {p.pk: key_str(p.exercise_id, p.name) for p in prescriptions}
    wanted = set(keys.values())
    if not wanted:
        return {}
    rows = {
        row.key: row
        for row in models.AthleteOneRm.objects.filter(
            athlete=athlete, key__in=wanted, unit=unit
        )
    }
    return {pk: rows[key] for pk, key in keys.items() if key in rows}


# ---------------------------------------------------------------------------
# Manual, server-persisted 1RM (Phase 2) — set/clear the athlete's own number.
# ---------------------------------------------------------------------------


def clean_manual_value(raw):
    """Validate a posted manual 1RM → ``(Decimal | None, ok)``.

    Blank/``None`` means *clear* it → ``(None, True)``. Otherwise the value must be
    a positive, finite number that fits the ``value`` column → the quantized
    ``Decimal``; anything else (non-numeric, non-finite, ≤ 0, or overflowing) is a
    reject → ``(None, False)``. The two ``None`` cases are told apart by the ``ok``
    flag. ``nan``/``inf`` (``json.loads`` accepts ``NaN``/``Infinity``) are caught
    by the finiteness check before ``_quantize`` — which would otherwise raise on
    a non-finite ``Decimal`` and turn a bad request into a 500.
    """
    if raw in (None, ""):
        return None, True
    num = _num(raw)
    if num is None or not math.isfinite(num) or num <= 0:
        return None, False
    value = _quantize(num)
    if not (Decimal("0") < value <= _MAX_VALUE):
        return None, False
    return value, True


def set_manual_one_rm(athlete, prescription, value, unit):
    """Persist (or clear) ``athlete``'s manually-entered 1RM for a lift.

    ``value`` is a validated ``Decimal`` (from :func:`clean_manual_value`) in
    ``unit`` — stored as a ``source=manual`` row that ``refresh_one_rms`` will
    never overwrite — or ``None`` to *clear* it. Clearing reverts the lift to its
    log-derived estimate: the manual row is removed and the value is re-derived
    from history immediately, so the helper doesn't briefly vanish. Returns the
    resulting stored row (the manual one, or the freshly re-derived logged one),
    or ``None`` when neither exists.
    """
    key = key_str(prescription.exercise_id, prescription.name)
    if value is None:
        models.AthleteOneRm.objects.filter(
            athlete=athlete, key=key, source=models.AthleteOneRm.Source.MANUAL
        ).delete()
        # Re-derive from logs so a cleared lift falls back to its logged estimate
        # rather than showing nothing until the next log save.
        refresh_one_rms(athlete, [prescription], unit)
        return models.AthleteOneRm.objects.filter(
            athlete=athlete, key=key, unit=unit
        ).first()
    row, _ = models.AthleteOneRm.objects.update_or_create(
        athlete=athlete,
        key=key,
        defaults={
            "exercise_id": prescription.exercise_id,
            "name": prescription.name,
            "value": value,
            "unit": unit,
            "source": models.AthleteOneRm.Source.MANUAL,
        },
    )
    return row
