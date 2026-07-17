"""Coach-scoped one-click demo data (first-time-UX Phase 2, decision Q3).

A brand-new coach can load a *populated* workspace — five athletes and one
built/delivered/logged individual program — to explore Meso before committing
real clients, then remove it in one click.

This is a thin, coach-scoped wrapper over the demo the ``seed_meso_demo``
management command stands up: it reuses that command's data (``ATHLETES`` /
``SAMPLE_PLAN`` / ``SAMPLE_LOG``) but creates everything **scoped to
the requesting coach** so two coaches never collide. Guardrails (Q3):

- **clearly labeled + fully removable** — demo relationships carry an
  ``is_demo`` flag; ``clear_demo`` removes exactly those (and the demo athlete
  users they hang off), never the coach's real data;
- **billing-neutral** — an ``is_demo`` link is not a billable seat
  (``CoachAthlete.billable`` / ``billing/access.py``), so loading the demo never
  trips the paywall;
- **no outbound email/push** — demo athletes are fake people: their address is
  non-routable and namespaced per coach, they carry the delivery-email opt-out,
  and the load delivers weeks at the **model layer** (a direct ``delivered_at``
  stamp), which — unlike the deliver *views* — notifies nobody.
"""

from datetime import date
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from store_project.users.models import User

from .management.commands.seed_meso_demo import ATHLETES
from .management.commands.seed_meso_demo import SAMPLE_LOG
from .management.commands.seed_meso_demo import SAMPLE_PLAN
from .management.commands.seed_meso_demo import _months_before
from .management.commands.seed_meso_demo import _years_before
from .management.commands.seed_meso_demo import build_block
from .models import AthleteProfile
from .models import CoachAthlete
from .models import Contraindication
from .models import LoggedSet
from .models import Mesocycle
from .models import Plan
from .models import Session
from .models import SessionLog
from .models import Unit
from .models import Week
from .one_rm import refresh_one_rms

#: Non-routable (RFC 6761 ``.invalid``) demo-athlete domain — guaranteed never to
#: receive mail. A per-coach subdomain keeps addresses unique across coaches.
DEMO_EMAIL_DOMAIN = "demo.invalid"


def demo_email(coach, slug):
    """A per-coach, non-routable demo-athlete address (collision-free across coaches)."""
    return f"{slug}@{coach.pk.hex}.{DEMO_EMAIL_DOMAIN}"


def _demo_athletes(coach):
    """The coach's demo athlete users (via their ``is_demo`` links)."""
    return User.objects.filter(
        coach_links__coach=coach, coach_links__is_demo=True
    ).distinct()


def has_demo(coach):
    """Whether this coach currently has demo data loaded."""
    return has_athletes(coach)


def _lock(coach):
    """Serialize concurrent loads for this coach (double-submit protection).

    A per-coach row lock — real only inside a transaction, on a backend that
    supports it (Postgres); a no-op on the SQLite test DB, where requests
    don't race anyway. Called at the top of **every** segment loader, not just
    the aggregate: once the guided tour (Phase 2) wires each segment to its own
    POST endpoint, a segment can be loaded on its own — with no ``load_demo``
    call wrapping it — so it needs the same double-submit protection ``load_demo``
    always had. Re-acquiring the same row lock from nested segment calls within
    one transaction (e.g. ``load_log`` → ``load_delivery`` → ``load_program`` →
    ``load_athletes``) is harmless — it's the same connection re-affirming a
    lock it already holds, not a new wait.
    """
    User.objects.select_for_update().get(pk=coach.pk)


def _demo_athlete_and_link(coach, slug):
    """The demo athlete + link for ``slug``, assuming ``load_athletes`` already ran."""
    athlete = User.objects.get(email=demo_email(coach, slug))
    link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
    return athlete, link


# -- segment loaders ----------------------------------------------------------
#
# Per-feature slices of ``load_demo`` (guided-tour Phase 1, decision O3): each
# is idempotent and ensures its own prerequisites, so the tour (Phase 2) can
# fire any one of them, in any order, from its own step/endpoint. ``load_demo``
# below is the thin aggregate that runs all four — the O6 "skip · load
# everything" path and the pre-tour ``demo_load`` view behavior.


@transaction.atomic
def load_athletes(coach):
    """Segment: the 5 demo athlete users + active demo links. No prerequisites."""
    _lock(coach)
    today = date.today()
    for spec in ATHLETES:
        athlete = _ensure_demo_athlete(coach, spec, today)
        _ensure_demo_link(coach, athlete)


@transaction.atomic
def load_program(coach):
    """Segment: Maya's "Hypertrophy Block" plan tree. Depends on ``athletes``."""
    _lock(coach)
    load_athletes(coach)
    _, maya_link = _demo_athlete_and_link(coach, "maya")
    _ensure_demo_plan(maya_link)


@transaction.atomic
def load_delivery(coach):
    """Segment: deliver Maya's current week. Depends on ``program``."""
    _lock(coach)
    load_program(coach)
    _, maya_link = _demo_athlete_and_link(coach, "maya")
    plan = _ensure_demo_plan(maya_link)
    _ensure_demo_delivery(plan)


@transaction.atomic
def load_log(coach):
    """Segment: log Maya's Lower session + refresh her 1RM.

    Depends on ``delivery`` (the demo tells the coach workflow's story in
    order — deliver, then log — even though delivery no longer gates logging,
    2d), which in turn pulls in ``program``/``athletes``.
    """
    _lock(coach)
    load_delivery(coach)
    maya, maya_link = _demo_athlete_and_link(coach, "maya")
    plan = _ensure_demo_plan(maya_link)
    _ensure_demo_log(maya, plan, date.today())


#: Segment name → loader, for views to dispatch a per-segment load by POST field.
SEGMENTS = {
    "athletes": load_athletes,
    "program": load_program,
    "delivery": load_delivery,
    "log": load_log,
}


@transaction.atomic
def load_demo(coach):
    """Stand up (or top up) this coach's whole demo workspace. Idempotent.

    A thin aggregate over the segment loaders above — the O6 "skip · load
    everything" path and the pre-tour behavior of the ``demo_load`` view keep
    working exactly as before the split. Re-running never duplicates: every
    row is upserted by its natural key, the plan tree is only built when
    absent, and the demo week is delivered only once.

    Concurrency: see ``_lock`` — every segment loader locks the coach row
    itself, so this aggregate doesn't need to *also* hold its own lock for
    correctness. It still takes one anyway: a single lock acquisition up front
    means a concurrent ``load_demo`` retry blocks for the whole aggregate
    rather than interleaving segment-by-segment with another in-flight call.
    """
    _lock(coach)
    load_athletes(coach)
    load_program(coach)
    load_delivery(coach)
    load_log(coach)


# -- per-segment "is it loaded?" predicates ------------------------------------
#
# Mirrors ``has_demo``: loaded-ness is derived from data, never stored (O7), so
# the tour can ask "has this step's data already been added?" without its own
# state. Kept cheap (``exists()``), ``is_demo``-scoped.


def has_athletes(coach):
    """Whether this coach's demo athlete links are loaded."""
    return CoachAthlete.objects.for_coach(coach).filter(is_demo=True).exists()


def has_program(coach):
    """Whether Maya's demo plan tree has been built."""
    return Mesocycle.objects.filter(
        plan__relationship__coach=coach,
        plan__relationship__is_demo=True,
    ).exists()


def has_delivery(coach):
    """Whether Maya's demo current week has been delivered."""
    return Week.objects.filter(
        mesocycle__plan__relationship__coach=coach,
        mesocycle__plan__relationship__is_demo=True,
        delivered_at__isnull=False,
    ).exists()


def has_log(coach):
    """Whether Maya's demo session has been logged."""
    return SessionLog.objects.filter(athlete__in=_demo_athletes(coach)).exists()


@transaction.atomic
def clear_demo(coach):
    """Remove exactly this coach's demo data — never their real data.

    Deletes the demo athlete users, which cascades their links, individual
    plans, logged sessions, and profiles. A coach with no demo is a clean no-op.
    """
    demo_user_ids = list(_demo_athletes(coach).values_list("pk", flat=True))
    User.objects.filter(pk__in=demo_user_ids).delete()


# -- athletes + relationships ------------------------------------------------


def _ensure_demo_athlete(coach, spec, today):
    """A demo athlete user (namespaced, opted out of delivery email)."""
    email = demo_email(coach, spec["slug"])
    athlete, created = User.objects.get_or_create(
        email=email,
        defaults={
            "username": email,
            "name": spec["name"],
            "birthday": _years_before(today, spec["age"]),
        },
    )
    if created:
        athlete.set_unusable_password()
        athlete.save(update_fields=["password"])
    AthleteProfile.objects.update_or_create(
        user=athlete,
        defaults={
            "training_started": _months_before(today, spec["trained_months"]),
            # Demo athletes are fake people — never email them (belt-and-suspenders
            # alongside the non-routable address + model-layer delivery).
            "delivery_email_opt_out": True,
        },
    )
    for text in spec["contraindications"]:
        Contraindication.objects.get_or_create(
            athlete=athlete, text=text, defaults={"active": True}
        )
    return athlete


def _ensure_demo_link(coach, athlete):
    """An active, coach-invited **demo** link (idempotent, restored on reseed)."""
    link, _ = CoachAthlete.objects.update_or_create(
        coach=coach,
        athlete=athlete,
        defaults={
            "status": CoachAthlete.Status.ACTIVE,
            "invited_by": CoachAthlete.InvitedBy.COACH,
            "is_demo": True,
            "responded_at": None,
            "ended_at": None,
        },
    )
    return link


# -- the sample individual plan ----------------------------------------------


def _ensure_demo_plan(link):
    """Maya's sample plan rooted at her demo link (the full prototype grid)."""
    plan = link.working_plan()
    if plan is None:
        plan = Plan.objects.create(
            relationship=link,
            title=SAMPLE_PLAN["title"],
            goal=SAMPLE_PLAN["goal"],
            status=Plan.Status.ACTIVE,
            unit=Unit.KILOGRAMS,
        )
    if not plan.mesocycles.exists():
        _build_plan_tree(plan, SAMPLE_PLAN)
    return plan


def _build_plan_tree(plan, spec):
    """Materialize a fixed-lineup plan tree (P0) from a ``SAMPLE_PLAN``-shaped spec.

    A thin coach-scoped wrapper over ``seed_meso_demo.build_block`` — the same
    shared builder the owner demo uses — applied to a coach-scoped plan: for
    each mesocycle spec, create the ``Mesocycle`` then hand it to
    ``build_block`` to materialize the block's fixed lineup (``SessionSlot`` +
    ``ExerciseSlot``, once per block) and its ``Week``/``Prescription`` cells
    (only blocks with ``"days"``/``"weeks"`` materialize rows; the others are
    planned-length-only), mirroring how the designer renders one week at a time.
    """
    for meso_spec in spec["mesocycles"]:
        mesocycle = Mesocycle.objects.create(
            plan=plan,
            name=meso_spec["name"],
            order=meso_spec["order"],
            week_count=meso_spec["week_count"],
        )
        build_block(mesocycle, meso_spec)


def _demo_log_session(plan):
    """The ``Session`` ``SAMPLE_LOG`` describes (Maya's current-week "Lower" day).

    Shared by ``_ensure_demo_delivery`` and ``_ensure_demo_log`` — split out of
    the old combined ``_ensure_demo_log`` (guided-tour Phase 1) so "deliver the
    week" and "log the session" can be separate segment loaders. ``None`` only
    if the plan tree hasn't been built yet (the ``program`` segment never
    skipped in practice — every caller here ensures it first).
    """
    return (
        Session.objects.filter(
            week__mesocycle__plan=plan,
            week__mesocycle__name=SAMPLE_LOG["mesocycle"],
            week__index=SAMPLE_LOG["week_index"],
            session_slot__day_number=SAMPLE_LOG["day_number"],
        )
        .select_related("week", "session_slot")
        .first()
    )


def _ensure_demo_delivery(plan):
    """Deliver Maya's current-week session at the model layer (no notify).

    Idempotent: the week is delivered once. Stamps only ``Week.delivered_at``
    (no ``WeekDelivery`` snapshot) — matching the pre-split behavior; a full
    snapshot is the deliver *view*'s job, not the demo's.
    """
    session = _demo_log_session(plan)
    if session is None:
        return None
    week = session.week
    if week.delivered_at is None:
        week.delivered_at = timezone.now()
        week.save(update_fields=["delivered_at"])
    return week


def _ensure_demo_log(athlete, plan, today):
    """Log Maya's current-week session + refresh her derived 1RM (no notify).

    Idempotent: the log rows are created only when absent. Assumes the week is
    already delivered — the ``log`` segment loader ensures that itself via
    ``load_delivery`` before calling this; logging against an undelivered week
    doesn't error, it just wouldn't reflect the demo's real step order.
    """
    session = _demo_log_session(plan)
    if session is None:
        return None

    log, created = SessionLog.objects.get_or_create(
        session=session,
        athlete=athlete,
        defaults={
            "status": SessionLog.Status.DONE,
            "date": today - timedelta(days=SAMPLE_LOG["logged_days_ago"]),
        },
    )
    # ``session.cells()`` = this week's live Prescription cells for this day's
    # ExerciseSlot rows (replaces the old ``session.prescriptions``).
    prescriptions = {p.name: p for p in session.cells()}
    if not (not created and log.sets.exists()):
        log.sets.all().delete()
        rows = []
        for name, sets in SAMPLE_LOG["sets"].items():
            prescription = prescriptions.get(name)
            if prescription is None:
                continue
            for set_number, (reps, load, rpe) in enumerate(sets, start=1):
                rows.append(
                    LoggedSet(
                        session_log=log,
                        prescription=prescription,
                        set_number=set_number,
                        reps=reps,
                        load=load,
                        rpe=rpe,
                    )
                )
        LoggedSet.objects.bulk_create(rows)

    refresh_one_rms(athlete, list(prescriptions.values()), plan.unit)
    return log
