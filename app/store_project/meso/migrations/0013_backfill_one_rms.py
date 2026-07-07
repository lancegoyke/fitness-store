"""Backfill ``AthleteOneRm`` from existing completed logs (S2 1RM follow-up).

Going forward ``one_rm.refresh_one_rms`` keeps the estimate fresh on every log
save, but logs written *before* this slice have no row yet — so the coach
designer + athlete logger would show no 1RM until the athlete logged again. This
one-off pass derives the best Epley estimate per (athlete, lift) from every
``DONE`` logged set and creates the rows, so existing history (the demo seed's
logged session included) is reflected immediately. Idempotent-friendly: only
fills lifts that have no row yet, so a re-run never clobbers a fresher value.

Epley + the identity key are inlined (not imported from app code) so this
historical migration stays decoupled from the live model's helpers.
"""

from decimal import Decimal

from django.db import migrations


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epley(load, reps):
    w = _num(load)
    r = _num(reps)
    if w is None or r is None or w <= 0 or r < 1:
        return None
    return w if r == 1 else w * (1 + r / 30)


def _key_str(exercise_id, name):
    if exercise_id is not None:
        return f"id:{exercise_id}"
    return f"name:{(name or '').strip().lower()}"


def backfill(apps, schema_editor):
    LoggedSet = apps.get_model("meso", "LoggedSet")
    AthleteOneRm = apps.get_model("meso", "AthleteOneRm")

    # Newest log first: the most recent completed log for a lift fixes the unit
    # the stored estimate is denominated in (mirroring the runtime, where a fresh
    # log overwrites the row with its plan's unit). A logged ``load`` is a bare
    # number whose unit is the plan's, so only same-unit sets are pooled.
    logged_sets = (
        LoggedSet.objects.filter(
            session_log__status="done",
            prescription__isnull=False,
        )
        .select_related("session_log", "prescription")
        .order_by("-session_log__date", "-session_log__created_at")
    )

    # best[(athlete_id, key)] = {value, exercise_id, name, unit}
    best = {}
    for ls in logged_sets:
        presc = ls.prescription
        identity = (ls.session_log.athlete_id, _key_str(presc.exercise_id, presc.name))
        # Reach the plan's unit. The prescription's path to its week changed with
        # the fixed-lineup reshape (a per-week ``ExercisePrescription.session`` →
        # a ``Prescription`` cell with a direct ``.week``); resolve both so this
        # historical pass works against its own model state (a fresh DB migrating
        # up) and against the current schema (this codebase tests it live).
        week = getattr(presc, "week", None) or presc.session.week
        plan = week.mesocycle.plan
        entry = best.get(identity)
        if entry is None:
            # First (most recent) sighting fixes the unit + display name.
            entry = best[identity] = {
                "value": None,
                "exercise_id": presc.exercise_id,
                "name": presc.name,
                "unit": plan.unit,
            }
        if plan.unit != entry["unit"]:
            continue  # an older log of this lift in the other unit — not pooled
        est = _epley(ls.load, ls.reps)
        if est is None:
            continue
        if entry["value"] is None or est > entry["value"]:
            entry["value"] = est

    for (athlete_id, key), data in best.items():
        if data["value"] is None:
            continue  # the lift had no usable (numeric) logged set
        value = Decimal(str(round(data["value"], 2)))
        # Skip an absurd value the Decimal(7, 2) column can't hold (a fat-fingered
        # logged load) rather than crash the migration.
        if not (Decimal("0") < value <= Decimal("99999.99")):
            continue
        AthleteOneRm.objects.get_or_create(
            athlete_id=athlete_id,
            key=key,
            defaults={
                "exercise_id": data["exercise_id"],
                "name": data["name"],
                "value": value,
                "unit": data["unit"],
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("meso", "0012_athleteonerm"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
