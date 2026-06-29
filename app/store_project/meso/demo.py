"""Coach-scoped one-click demo data (first-time-UX Phase 2, decision Q3).

A brand-new coach can load a *populated* workspace — five athletes, one
built/delivered/logged individual program, and a training group with a shared
program plus per-athlete auto-adjusts — to explore Meso before committing real
clients, then remove it in one click.

This is a thin, coach-scoped wrapper over the demo the ``seed_meso_demo``
management command stands up: it reuses that command's data (``ATHLETES`` /
``SAMPLE_PLAN`` / ``SAMPLE_LOG`` / ``GROUP``) but creates everything **scoped to
the requesting coach** so two coaches never collide. Guardrails (Q3):

- **clearly labeled + fully removable** — demo relationships/groups carry an
  ``is_demo`` flag; ``clear_demo`` removes exactly those (and the demo athlete
  users they hang off), never the coach's real data;
- **billing-neutral** — an ``is_demo`` link is not a billable seat
  (``CoachAthlete.billable`` / ``billing/access.py``), so loading the demo never
  trips the paywall;
- **no outbound email/push** — demo athletes are fake people: their address is
  non-routable and namespaced per coach, they carry the delivery-email opt-out,
  and the load delivers weeks at the **model layer** (``deliver_current_week`` /
  a direct ``delivered_at`` stamp), which — unlike the deliver *views* — notifies
  nobody.
"""

from datetime import date
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from store_project.users.models import User

from .management.commands.seed_meso_demo import ATHLETES
from .management.commands.seed_meso_demo import GROUP
from .management.commands.seed_meso_demo import SAMPLE_LOG
from .management.commands.seed_meso_demo import SAMPLE_PLAN
from .management.commands.seed_meso_demo import _months_before
from .management.commands.seed_meso_demo import _years_before
from .models import AthleteProfile
from .models import CoachAthlete
from .models import Contraindication
from .models import ExercisePrescription
from .models import LoadType
from .models import LoggedSet
from .models import Mesocycle
from .models import MesoGroup
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
    return CoachAthlete.objects.for_coach(coach).filter(is_demo=True).exists()


@transaction.atomic
def load_demo(coach):
    """Stand up (or top up) this coach's demo workspace. Idempotent.

    Re-running never duplicates: every row is upserted by its natural key, the
    plan tree is only built when absent, and the demo week is delivered only once.
    """
    today = date.today()
    athletes = {}
    for spec in ATHLETES:
        athlete = _ensure_demo_athlete(coach, spec, today)
        link = _ensure_demo_link(coach, athlete)
        athletes[spec["slug"]] = (athlete, link)
    maya, maya_link = athletes["maya"]
    plan = _ensure_demo_plan(maya_link)
    _ensure_demo_log(maya, plan, today)
    _ensure_demo_group(coach, athletes)


@transaction.atomic
def clear_demo(coach):
    """Remove exactly this coach's demo data — never their real data.

    The demo group is owned by the (real) coach, so it won't cascade from deleting
    the demo athletes; drop it explicitly first (cascading its shared +
    materialized plans, memberships, and overrides). Then delete the demo athlete
    users, which cascades their links, individual plans, logged sessions, and
    profiles. A coach with no demo is a clean no-op.
    """
    MesoGroup.objects.filter(coach=coach, is_demo=True).delete()
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
    """Materialize a ``Mesocycle → Week → Session → ExercisePrescription`` tree.

    The same shape ``seed_meso_demo`` builds for the owner demo, applied to a
    coach-scoped plan. Only blocks with ``weeks`` materialize rows (the others are
    planned-length-only), mirroring how the designer renders one week at a time.
    """
    for meso_spec in spec["mesocycles"]:
        mesocycle = Mesocycle.objects.create(
            plan=plan,
            name=meso_spec["name"],
            order=meso_spec["order"],
            week_count=meso_spec["week_count"],
        )
        for week_spec in meso_spec["weeks"]:
            week = Week.objects.create(
                mesocycle=mesocycle,
                index=week_spec["index"],
                phase=week_spec["phase"],
                volume=week_spec["volume"],
                intensity=week_spec["intensity"],
                is_deload=week_spec["is_deload"],
                is_current=week_spec["is_current"],
            )
            for order, sess_spec in enumerate(week_spec["sessions"]):
                session = Session.objects.create(
                    week=week,
                    day_number=sess_spec["day_number"],
                    name=sess_spec["name"],
                    bias=sess_spec["bias"],
                    order=order,
                )
                for ex_order, ex in enumerate(sess_spec["exercises"]):
                    ExercisePrescription.objects.create(
                        session=session,
                        name=ex["name"],
                        order=ex_order,
                        sets=ex.get("sets", ""),
                        reps=ex.get("reps", ""),
                        load=ex.get("load", ""),
                        load_type=ex.get("load_type", LoadType.ABSOLUTE),
                        rpe=ex.get("rpe", ""),
                        note=ex.get("note", ""),
                        tags=ex.get("tags", []),
                    )


def _ensure_demo_log(athlete, plan, today):
    """Deliver + log Maya's current-week session at the model layer (no notify).

    Idempotent: the week is delivered once and the log rows are created only when
    absent. Refreshes the athlete's derived 1RM so the demo's %1RM lift shows one.
    """
    session = (
        Session.objects.filter(
            week__mesocycle__plan=plan,
            week__mesocycle__name=SAMPLE_LOG["mesocycle"],
            week__index=SAMPLE_LOG["week_index"],
            day_number=SAMPLE_LOG["day_number"],
        )
        .select_related("week")
        .first()
    )
    if session is None:
        return None

    week = session.week
    if week.delivered_at is None:
        week.delivered_at = timezone.now()
        week.save(update_fields=["delivered_at"])

    log, created = SessionLog.objects.get_or_create(
        session=session,
        athlete=athlete,
        defaults={
            "status": SessionLog.Status.DONE,
            "date": today - timedelta(days=SAMPLE_LOG["logged_days_ago"]),
        },
    )
    prescriptions = {
        p.name: p for p in ExercisePrescription.objects.filter(session=session)
    }
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


# -- the demo group ----------------------------------------------------------


def _ensure_demo_group(coach, athletes):
    """A demo group (three members) with a shared program, overrides, delivery."""
    group, _ = MesoGroup.objects.update_or_create(
        coach=coach,
        name=GROUP["name"],
        defaults={
            "focus": GROUP["focus"],
            "status": MesoGroup.Status.ACTIVE,
            "is_demo": True,
        },
    )
    memberships = {}
    for slug in GROUP["member_slugs"]:
        athlete, _link = athletes[slug]
        memberships[slug] = group.add_athlete(athlete)
    if group.shared_plan() is None:
        group.create_shared_plan()
    _ensure_demo_overrides(group, memberships)
    _ensure_demo_group_delivery(group)
    return group


def _ensure_demo_overrides(group, memberships):
    """A couple of per-athlete auto-adjusts so the designer's ``adj`` badge shows."""
    plan = group.shared_plan()
    if plan is None:
        return
    prescriptions = list(
        ExercisePrescription.objects.filter(
            session__week__mesocycle__plan=plan
        ).order_by("session__order", "order")
    )
    if len(prescriptions) < 2:
        return
    first, second = prescriptions[0], prescriptions[1]
    if "devon" in memberships:
        memberships["devon"].set_override(first, load_pct=90)
    if "priya" in memberships:
        memberships["priya"].set_override(first, swap_name="Box Squat")
    if "marcus" in memberships:
        memberships["marcus"].set_override(second, sets="2", reps="8")


def _ensure_demo_group_delivery(group):
    """Deliver the shared current week to members once (model layer — no notify)."""
    from .serializers import current_week

    plan = group.shared_plan()
    if plan is None:
        return
    week = current_week(plan)
    if week is None or week.delivered_at is not None:
        return
    group.deliver_current_week()
