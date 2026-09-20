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
from django.db.models import Q
from django.utils import timezone

from store_project.users.models import User

from .management.commands.seed_meso_demo import ATHLETES
from .management.commands.seed_meso_demo import SAMPLE_LOG
from .management.commands.seed_meso_demo import SAMPLE_PLAN
from .management.commands.seed_meso_demo import _months_before
from .management.commands.seed_meso_demo import _years_before
from .management.commands.seed_meso_demo import build_block
from .models import AgentProposalBatch
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


def lock_cascade_parents(user_ids):
    """Take the app-wide parent row locks a cascade delete of ``user_ids`` will reach (#559).

    Must run inside the caller's transaction, immediately BEFORE the
    ``.delete()`` it protects. Shared with ``sandbox.expire_sandboxes``, which
    reaps the sandbox coach's own rows the same way.

    Deliberately NOT ``@transaction.atomic``, and the omission is load-bearing:
    a decorator here would let a standalone call look like it worked — taking
    every lock, then committing and releasing all of them before the delete it
    was meant to cover ever ran. Without one, such a call raises instead. (An
    earlier revision of this function did carry the decorator, by accident: it
    was inserted directly beneath ``clear_demo``'s own ``@transaction.atomic``
    and took it over. ``clear_demo`` now states its transaction with an
    explicit ``with`` block, where the comment explaining why it is required
    can sit next to it.)

    Django's ``Collector.delete`` walks the tree CHILD-FIRST: it fast-deletes
    ``ProposedChange`` and ``Prescription`` rows (``DELETE ... WHERE
    batch_id IN (...)``), then runs the ``SET_NULL`` updates, then deletes the
    parents — so a plain cascade delete acquires its row locks in the exact
    reverse of every edit path, all of which lock a parent and then write its
    children (``record_plan_action`` takes the ``Plan`` before the designer
    writes a cell; ``batch_apply`` takes the ``Plan`` and then the batch). Two
    of those overlapping is a lock cycle, and PostgreSQL resolves it by
    aborting one side with ``deadlock detected`` — a 500 on whichever request
    it picks, usually the coach's edit. The reachable shape: a coach with a
    demo athlete's review screen open in one tab clicks "Remove demo data" in
    another and approves a change in the same instant.

    Locking the parents FIRST, in the app-wide order
    (``docs/meso/decisions.md`` — ``User``, then ``CoachAthlete``, then
    ``Plan``, then ``AgentProposalBatch``, ascending pk within each table),
    removes the cycle instead of narrowing it: an edit that arrives afterwards
    waits on the parent lock, and once this delete commits it re-reads and
    answers cleanly (a 404/409 for a row that is now gone) rather than dying in
    a deadlock.

    That clean answer is a promise about paths taking an EXPLICIT lock, which
    is what makes them re-read after the wait. A path that merely INSERTs a row
    referencing one of these — ``agent_propose`` → ``create_drafting_batch``
    never locks the ``Plan`` — does all its work, waits at COMMIT on the
    deferred FK instead, and then raises ``IntegrityError`` against a ``Plan``
    that no longer exists: a 500, not a 404. Removing the deadlock does not
    make every loser graceful.

    IT HAS TO START AT ``User``/``CoachAthlete``, not at ``Plan``, and not only
    because a cascade delete reaches those two levels as well. Each query below
    is one statement's snapshot, and under READ COMMITTED the collector's own
    later SELECTs take fresh ones — so a ``Plan`` INSERTed and committed after
    the plan query runs is still COLLECTED by the delete while nothing holds its
    row lock, and the delete then takes that plan's children before the plan
    itself: the very cycle this function exists to remove, re-opened. It is
    reachable: ``views.plan_create`` locks only the ``CoachAthlete`` link before
    inserting a plan, so a coach who clicks "Remove demo data" in one tab and
    creates a plan for a plan-less demo athlete in another gets exactly that
    state. Locking the LINK rows first closes it — ``plan_create`` then waits on
    the link it needs and, once this delete commits, finds it gone and answers
    404 — which no amount of care in the plan query itself could do, because the
    row it would need to see does not exist yet when that query runs.

    ``no_key=True`` ON EVERY LEVEL, even though these rows are about to be
    DELETEd and a DELETE takes ``FOR UPDATE`` anyway. Nothing here is deleting
    yet — this function only RESERVES the rows, in order, ahead of a
    ``.delete()`` that takes its own stronger locks when it runs. ``FOR NO KEY
    UPDATE`` gives exactly the exclusion the ordering needs: it conflicts with
    every other writer's ``FOR UPDATE`` and ``FOR NO KEY UPDATE``, so a
    concurrent edit still waits.

    What it deliberately does NOT conflict with is the ``FOR KEY SHARE`` a
    transaction takes on a parent row at COMMIT to check a deferred FK — Django
    emits every PostgreSQL FK ``DEFERRABLE INITIALLY DEFERRED`` — and holding
    ``FOR UPDATE`` across a later lock acquisition here made that a deadlock
    generator rather than a hazard. The concrete cycle, which plain
    ``FOR UPDATE`` on step 1 really did produce: a coach loads a demo segment
    (``load_log``) in one tab, which takes ``_lock(coach)`` and then
    ``CoachAthlete.objects.update_or_create`` — itself a
    ``select_for_update().get_or_create()`` — and holds that link while it
    INSERTs a ``SessionLog`` and an ``AthleteOneRm`` for the demo athlete;
    "Remove demo data" in another tab then locks that athlete's ``User`` row
    (unheld: the loader's FK check is deferred), blocks on the link at step 2,
    and the loader's COMMIT then wants ``FOR KEY SHARE`` on the very ``User``
    row step 1 holds. That cycle does not exist without a lock at all, so it
    would have been introduced BY this fix. ``no_key=True`` removes it while
    leaving every ordering property intact. This is #560's lesson, one level
    up: the strength that matters is the one a deferred FK check will want.

    The cost, stated rather than glossed: a child row INSERTed concurrently by
    such a transaction can be missed by the collector's own SELECT and leave the
    delete to fail its deferred FK check at commit. That is exactly what
    happens today with no lock at all, so it is not a regression — and a
    deadlock aborts somebody's request either way, while this one at worst
    aborts the delete that chose to run.

    ``.order_by("pk")`` puts the acquisition order in ascending pk:
    PostgreSQL's ``LockRows`` node sits above the sort, so rows are locked in
    the order they come out.

    Every filter below is on a LOCAL column — the link pks feed the plan query
    rather than joining ``CoachAthlete``, and the mesocycle pks feed the batch
    query rather than joining ``Mesocycle`` — so no query here needs
    ``of=("self",)`` and none can lock a joined row by accident. That also
    sidesteps a sharper edge: ``Plan.relationship`` is nullable, so a joined
    form promotes to a LEFT OUTER JOIN, and a bare ``FOR UPDATE`` over one of
    those is a hard PostgreSQL error rather than a silent over-lock.
    """
    user_ids = list(user_ids)
    if not user_ids:
        return
    # 1. The users themselves — the roots the cascade deletes last.
    list(
        User.objects.select_for_update(no_key=True)
        .filter(pk__in=user_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    # 2. Their coach<->athlete links, as athlete OR as coach: reaping a sandbox
    #    coach deletes links where they are the coach, clearing demo data
    #    deletes links where the demo user is the athlete.
    link_pks = list(
        CoachAthlete.objects.select_for_update(no_key=True)
        .filter(Q(athlete_id__in=user_ids) | Q(coach_id__in=user_ids))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    # 3. The plans hanging off those links, plus any template plan these users
    #    own outright (``Plan.owner`` is its own CASCADE FK).
    plan_pks = list(
        Plan.objects.select_for_update(no_key=True)
        .filter(Q(relationship_id__in=link_pks) | Q(owner_id__in=user_ids))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    # 4. The batches. Three ways one is reached, not just the obvious one:
    #    through its ``plan``; through ``coach``, its own CASCADE FK to
    #    ``User``; and through ``mesocycle``, which is SET_NULL — the collector
    #    UPDATEs those rows rather than deleting them, and an UPDATE takes an
    #    exclusive row lock just the same. A batch normally has
    #    ``mesocycle.plan_id == plan_id`` so that third clause is redundant,
    #    but ``plan`` is a ``raw_id_field`` on both the mesocycle and batch
    #    admins, so a staff re-point can separate them. The mesocycle pks
    #    themselves are read UNLOCKED, and holding the plans is NOT the reason
    #    that is safe — a write re-pointing a batch INTO this set takes its
    #    deferred FK's ``FOR KEY SHARE`` on the ``Mesocycle`` row, not on the
    #    ``Plan``. It is safe because only an admin raw-id re-point can do it,
    #    and that write takes no other lock we hold, so no cycle follows.
    mesocycle_pks = list(
        Mesocycle.objects.filter(plan_id__in=plan_pks).values_list("pk", flat=True)
    )
    list(
        AgentProposalBatch.objects.select_for_update(no_key=True)
        .filter(
            Q(plan_id__in=plan_pks)
            | Q(coach_id__in=user_ids)
            | Q(mesocycle_id__in=mesocycle_pks)
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def clear_demo(coach):
    """Remove exactly this coach's demo data — never their real data.

    Deletes the demo athlete users, which cascades their links, individual
    plans, logged sessions, and profiles. A coach with no demo is a clean no-op.

    The cascade's parents are row-locked first, in the app-wide order, so a
    concurrent coach edit on the same demo plan or batch waits instead of
    deadlocking with it — see ``lock_cascade_parents`` (#559).

    The ``with transaction.atomic()`` block replaces the ``@transaction.atomic``
    decorator this function used to carry — same transaction, stated where the
    reason can sit beside it, and not a place to tidy back into a decorator.
    That transaction is what makes the locks outlive the query that took them:
    ``Collector.delete`` opens a transaction of its own when there isn't one,
    which would release every lock before the delete they exist to cover ran.
    """
    with transaction.atomic():
        # Read the athlete set INSIDE the transaction that locks it, not before:
        # two statements in one transaction is still two snapshots under READ
        # COMMITTED, but a read taken outside it can be arbitrarily stale.
        demo_user_ids = list(_demo_athletes(coach).values_list("pk", flat=True))
        if not demo_user_ids:
            return
        lock_cascade_parents(demo_user_ids)
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
                        # #578 C1: written alongside `prescription`, not
                        # instead of it — see `LoggedSet.exercise_slot`'s
                        # model comment.
                        exercise_slot_id=prescription.exercise_slot_id,
                        set_number=set_number,
                        reps=reps,
                        load=load,
                        rpe=rpe,
                    )
                )
        LoggedSet.objects.bulk_create(rows)

    refresh_one_rms(athlete, list(prescriptions.values()), plan.unit)
    return log
