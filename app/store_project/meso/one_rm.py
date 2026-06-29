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
``docs/meso/one-rm-plan.md``.
"""

from decimal import Decimal

from . import models
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


def derive_one_rm_values(athlete, *, keys=None, unit=None):
    """Best Epley 1RM per lift identity from the athlete's *completed* logged sets.

    One query over the athlete's ``DONE`` logged sets (a pending "Save progress"
    draft is not a finished performance — the results/"last" surfaces treat it the
    same). Returns ``{key: float}`` — the maximum implied 1RM across every set of
    that lift. ``keys``, when given, restricts the scan to those lift identities
    (the lifts in a session just logged); a lift with no usable set is absent.

    ``unit`` scopes the scan to logged sets from plans in that unit — a logged
    ``load`` is a bare number whose unit is the plan's, so pooling kg and lb sets
    for one lift would be unit-confused. The estimate is therefore derived (and
    stored) per unit.
    """
    logged_sets = models.LoggedSet.objects.filter(
        session_log__athlete=athlete,
        session_log__status=models.SessionLog.Status.DONE,
        prescription__isnull=False,
    ).select_related("prescription")
    if unit is not None:
        logged_sets = logged_sets.filter(
            session_log__session__week__mesocycle__plan__unit=unit
        )
    best = {}
    for ls in logged_sets:
        key = key_str(ls.prescription.exercise_id, ls.prescription.name)
        if keys is not None and key not in keys:
            continue
        est = epley_one_rm(ls.load, ls.reps)
        if est is None:
            continue
        if key not in best or est > best[key]:
            best[key] = est
    return best


# The largest value the ``value`` column (``Decimal(7, 2)``) can hold. A derived
# estimate beyond this is a fat-fingered logged load, not a real 1RM — skip it
# rather than let a ``DecimalField`` overflow roll back the athlete's whole log
# (refresh runs inside the log-save transaction).
_MAX_VALUE = Decimal("99999.99")


def _quantize(value):
    """A derived float as a 2-decimal ``Decimal`` for the ``value`` column."""
    return Decimal(str(round(float(value), 2)))


def refresh_one_rms(athlete, prescriptions, unit):
    """Recompute + persist ``athlete``'s 1RM for the lifts in ``prescriptions``.

    Called after a log save: for each lift identity among ``prescriptions``,
    upsert the ``AthleteOneRm`` row to the freshly derived best Epley estimate
    over *all* the athlete's completed logs for that lift (not just this session —
    the 1RM is a property of the athlete, not one plan). A lift with no usable
    logged set yet (no numeric load/reps anywhere) is left untouched rather than
    written as null. ``unit`` records what the stored value is denominated in.
    """
    # One representative (exercise_id, name) per identity — a later prescription's
    # name wins for display, harmless since they share the identity.
    reps_by_key = {}
    for p in prescriptions:
        reps_by_key[key_str(p.exercise_id, p.name)] = (p.exercise_id, p.name)
    if not reps_by_key:
        return
    # Derive from same-unit logs only, so the stored value is unambiguously in
    # ``unit`` (the unit it's written with).
    derived = derive_one_rm_values(athlete, keys=set(reps_by_key), unit=unit)
    for key, (exercise_id, name) in reps_by_key.items():
        value = derived.get(key)
        quantized = _quantize(value) if value is not None else None
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
            },
        )


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
