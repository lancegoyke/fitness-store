"""Seed the Meso coach-side demo: a coach, athletes, relationships, a plan.

Phase 5 of the persistence slice (``docs/meso/persistence-plan.md``) retires the
client-side mock for the coach-side screens. This command stands up the same
demo the prototype showed — **now real, DB-backed rows** — so a fresh dev
database renders the roster, athlete profile, and designer from actual data:

- a demo **coach** (you) with a ``CoachProfile`` (programming voice);
- five demo **athletes** (the prototype's Maya / Devon / Priya / Marcus / Lena)
  as ``User`` rows with ``AthleteProfile`` + global ``Contraindication`` rows;
- an **active** ``CoachAthlete`` link per athlete (coach-invited, accepted);
- one sample **Plan** for Maya — the full ``Mesocycle → Week → Session →
  ExercisePrescription`` hierarchy reproducing the designer's fixture grid, so
  ``serialize_plan`` round-trips it straight into the designer;
- one demo **MesoGroup** (groups slice S1) with three of the athletes as
  members + a **shared program** rooted at the group (Phase 2a) carrying a couple
  of per-athlete **auto-adjusts** (Phase 3), so the roster's *Groups* card and the
  group designer (including its ``adj`` badge) all render off real rows.

The command is **idempotent**: re-running ``get_or_create``s every row, so it
never duplicates. ``--delete`` tears the demo back down (the demo athletes and,
by cascade, their links and plans) for a clean re-seed. Maya's current-week
"Lower" session is delivered and **logged** so the coach's results screen and
the designer's "last time" column light up off real data (athlete slice Phase
3); the review screen renders real agent batches once a proposal is run.
"""

from datetime import date
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.core.management.base import CommandParser
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import get_random_string

from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachProfile
from store_project.meso.models import Contraindication
from store_project.meso.models import ExercisePrescription
from store_project.meso.models import LoadType
from store_project.meso.models import LoggedSet
from store_project.meso.models import Mesocycle
from store_project.meso.models import MesoGroup
from store_project.meso.models import Plan
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.models import Unit
from store_project.meso.models import Week
from store_project.meso.one_rm import refresh_one_rms
from store_project.users.models import User

DEFAULT_COACH_EMAIL = "lancegoyke@gmail.com"

# The coach's programming voice (the prototype's COACH_STYLE).
COACH_STYLE_TAGS = [
    "Compound-first",
    "RPE-based load",
    "Free-weight bias",
    "2-min rest cap",
    "Unilateral work",
]
COACH_AVOID = (
    "machine-only days, untracked progressions, >3 exercises to failure / session."
)

# The five demo athletes (the prototype's roster). ``trained_months`` and
# ``age`` are stored as derived dates so the profile screen reads them back the
# way the mock did ("14 mo trained", "34").
ATHLETES = [
    {
        "slug": "maya",
        "name": "Maya Okonkwo",
        "email": "maya.okonkwo@example.com",
        "age": 34,
        "trained_months": 14,
        "contraindications": [
            "L knee — avoid deep knee flexion under load",
            "No max-effort jumping / impact",
        ],
    },
    {
        "slug": "devon",
        "name": "Devon Reyes",
        "email": "devon.reyes@example.com",
        "age": 28,
        "trained_months": 6,
        "contraindications": ["R shoulder — neutral-grip pressing only"],
    },
    {
        "slug": "priya",
        "name": "Priya Nair",
        "email": "priya.nair@example.com",
        "age": 41,
        "trained_months": 72,
        "contraindications": [],
    },
    {
        "slug": "marcus",
        "name": "Marcus Tan",
        "email": "marcus.tan@example.com",
        "age": 35,
        "trained_months": 36,
        "contraindications": [],
    },
    {
        "slug": "lena",
        "name": "Lena Kovic",
        "email": "lena.kovic@example.com",
        "age": 31,
        "trained_months": 24,
        "contraindications": [
            "Lower back — trap-bar / RDL only, no conventional pull",
        ],
    },
]

# Maya's sample plan — the designer's fixture grid, as real rows. Only the
# current block ("Hypertrophy") materializes Week rows, and only the current
# week (Wk 2) materializes sessions, mirroring how the designer renders one
# week at a time; the other blocks are planned-length-only (week_count).
SAMPLE_PLAN = {
    "title": "Hypertrophy Block",
    "goal": "Hypertrophy",
    "mesocycles": [
        {"name": "Base / GPP", "order": 0, "week_count": 4, "weeks": []},
        {
            "name": "Hypertrophy",
            "order": 1,
            "week_count": 4,
            "weeks": [
                {
                    "index": 1,
                    "phase": "Accum",
                    "volume": 70,
                    "intensity": 62,
                    "is_deload": False,
                    "is_current": False,
                    "sessions": [],
                },
                {
                    "index": 2,
                    "phase": "Accum",
                    "volume": 85,
                    "intensity": 68,
                    "is_deload": False,
                    "is_current": True,
                    "sessions": [
                        {
                            "day_number": 1,
                            "name": "Lower",
                            "bias": "Quad bias · knee-safe",
                            "exercises": [
                                {
                                    # Prescribed as a % of 1RM (S2) — the demo row
                                    # that shows the %-vs-unit Load typing.
                                    "name": "Box Squat (to parallel)",
                                    "sets": "4",
                                    "reps": "6",
                                    "load": "72",
                                    "load_type": LoadType.PERCENT,
                                    "rpe": "7",
                                    "tags": ["knee-safe"],
                                },
                                {
                                    "name": "Bulgarian Split Squat (DB)",
                                    "sets": "3",
                                    "reps": "10",
                                    "load": "18",
                                    "rpe": "7",
                                },
                                {
                                    "name": "Leg Press (controlled ROM)",
                                    "sets": "3",
                                    "reps": "12",
                                    "load": "110",
                                    "rpe": "8",
                                },
                                {
                                    "name": "Seated Leg Curl",
                                    "sets": "3",
                                    "reps": "12",
                                    "load": "41",
                                    "rpe": "8",
                                },
                                {
                                    "name": "Standing Calf Raise",
                                    "sets": "4",
                                    "reps": "15",
                                    "load": "60",
                                    "rpe": "—",
                                },
                            ],
                        },
                        {
                            "day_number": 2,
                            "name": "Upper",
                            "bias": "Push / pull",
                            "exercises": [
                                {
                                    "name": "Incline DB Press",
                                    "sets": "4",
                                    "reps": "8",
                                    "load": "24",
                                    "rpe": "7",
                                    "note": "monitor shoulder",
                                },
                                {
                                    "name": "Chest-Supported Row",
                                    "sets": "4",
                                    "reps": "10",
                                    "load": "27",
                                    "rpe": "7",
                                },
                                {
                                    "name": "Lat Pulldown",
                                    "sets": "3",
                                    "reps": "12",
                                    "load": "52",
                                    "rpe": "8",
                                },
                                {
                                    "name": "DB Shoulder Press",
                                    "sets": "3",
                                    "reps": "10",
                                    "load": "16",
                                    "rpe": "7",
                                    "note": "neutral grip",
                                },
                                {
                                    "name": "Cable Lateral Raise",
                                    "sets": "3",
                                    "reps": "15",
                                    "load": "9",
                                    "rpe": "—",
                                },
                            ],
                        },
                        {
                            "day_number": 3,
                            "name": "Posterior",
                            "bias": "Hinge",
                            "exercises": [
                                {
                                    "name": "Trap-Bar Deadlift",
                                    "sets": "4",
                                    "reps": "6",
                                    "load": "92.5",
                                    "rpe": "7",
                                },
                                {
                                    "name": "Hip Thrust",
                                    "sets": "3",
                                    "reps": "10",
                                    "load": "80",
                                    "rpe": "8",
                                },
                                {
                                    "name": "Romanian Deadlift (3-1-1)",
                                    "sets": "3",
                                    "reps": "8",
                                    "load": "60",
                                    "rpe": "7",
                                    "note": "tempo eccentric",
                                },
                                {
                                    "name": "Reverse Lunge (DB)",
                                    "sets": "3",
                                    "reps": "12",
                                    "load": "14",
                                    "rpe": "—",
                                    "note": "knee-monitored",
                                    "tags": ["knee-safe"],
                                },
                                {
                                    "name": "Hanging Knee Raise",
                                    "sets": "3",
                                    "reps": "12",
                                    "load": "BW",
                                    "rpe": "—",
                                },
                            ],
                        },
                    ],
                },
                {
                    "index": 3,
                    "phase": "Accum",
                    "volume": 100,
                    "intensity": 73,
                    "is_deload": False,
                    "is_current": False,
                    "sessions": [],
                },
                {
                    "index": 4,
                    "phase": "Deload",
                    "volume": 55,
                    "intensity": 70,
                    "is_deload": True,
                    "is_current": False,
                    "sessions": [],
                },
            ],
        },
        {"name": "Strength", "order": 2, "week_count": 4, "weeks": []},
        {"name": "Peak / Test", "order": 3, "week_count": 2, "weeks": []},
    ],
}

# Maya's logged "Lower" session (the current week, Day 1) — the first real logged
# rows on the demo. Worked mostly to target, with the Box Squat top set running
# hot and the last leg-curl set falling short, so the results screen shows a real
# completion %, an RPE-over flag, and a shortfall note. ``(reps, load, rpe)`` per
# set, keyed by the prescription's name.
SAMPLE_LOG = {
    "mesocycle": "Hypertrophy",
    "week_index": 2,
    "day_number": 1,
    "logged_days_ago": 2,
    "sets": {
        "Box Squat (to parallel)": [
            ("6", "70", "7"),
            ("6", "70", "7"),
            ("6", "70", "7"),
            ("6", "70", "8.5"),
        ],
        "Bulgarian Split Squat (DB)": [("10", "18", "7")] * 3,
        "Leg Press (controlled ROM)": [("12", "110", "8")] * 3,
        "Seated Leg Curl": [("12", "41", "8"), ("12", "41", "8"), ("9", "41", "8.5")],
        "Standing Calf Raise": [("15", "60", "")] * 4,
    },
}


# A demo group (groups slice S1): three of the athletes who train together, so a
# fresh DB renders the roster's *Groups* card off real rows. Phase 2a also gives
# it a shared program (rooted at the group); per-athlete auto-adjusts are Phase 3.
GROUP = {
    "name": "Tue/Thu Strength Squad",
    "focus": "Strength",
    "member_slugs": ["devon", "priya", "marcus"],
}

# A pending email invite (N4) so the roster's onboarding surface is visible — a
# person the coach invited who hasn't claimed an account yet.
PENDING_INVITE_EMAIL = "prospect@example.com"

# A pending athlete→coach request (N4 Phase 2) so the roster's request surface is
# visible — an existing user who has asked to train under the coach.
PENDING_REQUEST_EMAIL = "hopeful@example.com"
PENDING_REQUEST_NAME = "Hopeful Newcomer"


def _months_before(today, months):
    """The date ``months`` whole months before ``today`` (day clamped to ≤28)."""
    total = today.year * 12 + (today.month - 1) - months
    year, month = divmod(total, 12)
    return date(year, month + 1, min(today.day, 28))


def _years_before(today, years):
    """The date ``years`` years before ``today`` (day clamped to ≤28)."""
    return date(today.year - years, today.month, min(today.day, 28))


class Command(BaseCommand):
    help = "Seed the Meso coach-side demo (coach, athletes, relationships, a plan)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--coach-email",
            default=DEFAULT_COACH_EMAIL,
            help=f"Email of the demo coach (default: {DEFAULT_COACH_EMAIL}).",
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Tear down the demo (athletes + their links and plans) and exit.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        coach_email = options["coach_email"]

        if options["delete"]:
            self._delete_demo(coach_email)
            return

        today = date.today()
        coach = self._ensure_coach(coach_email)
        for spec in ATHLETES:
            athlete = self._ensure_athlete(spec, today)
            self._ensure_link(coach, athlete)
            if spec["slug"] == "maya":
                plan = self._ensure_plan(coach, athlete)
                self._ensure_log(athlete, plan, today)
        self._ensure_group(coach)
        self._ensure_pending_invite(coach)
        self._ensure_pending_request(coach)

        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Meso demo seeded for {coach.email}: "
                f"{len(ATHLETES)} athletes, 1 group (+ shared program), "
                "1 sample plan, 1 logged session, 1 pending invite, "
                "1 pending request."
            )
        )

    # -- teardown ---------------------------------------------------------

    def _delete_demo(self, coach_email):
        # The demo group is owned by the (kept) coach, so deleting the demo
        # athletes only cascade-removes its memberships; drop the group too.
        MesoGroup.objects.filter(coach__email=coach_email, name=GROUP["name"]).delete()
        CoachInvite.objects.filter(
            coach__email=coach_email, email=PENDING_INVITE_EMAIL
        ).delete()
        # Drop the requester (their pending request link cascades with the user).
        User.objects.filter(email=PENDING_REQUEST_EMAIL).delete()
        emails = [spec["email"] for spec in ATHLETES]
        deleted, _ = User.objects.filter(email__in=emails).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Meso demo torn down ({deleted} rows; demo athletes, links, plans)."
            )
        )

    # -- coach ------------------------------------------------------------

    def _ensure_coach(self, email):
        coach, created = User.objects.get_or_create(
            email=email,
            defaults={"username": email, "name": "Lance Goyke"},
        )
        if created:
            # Fresh dev DB only: a usable, throwaway password printed once so you
            # can log in. An existing coach (the common case) keeps their own.
            password = get_random_string(16)
            coach.set_password(password)
            coach.save(update_fields=["password"])
            self.stdout.write(
                f"  - created coach {email} (temporary password: {password})"
            )
        else:
            self.stdout.write(f"  - using existing coach {email}")

        CoachProfile.objects.update_or_create(
            user=coach,
            defaults={
                "programming_style": COACH_STYLE_TAGS,
                "avoid_rules": COACH_AVOID,
                "default_unit": Unit.KILOGRAMS,
            },
        )
        return coach

    # -- athletes ---------------------------------------------------------

    def _ensure_athlete(self, spec, today):
        athlete, created = User.objects.get_or_create(
            email=spec["email"],
            defaults={
                "username": spec["email"],
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
            },
        )
        for text in spec["contraindications"]:
            Contraindication.objects.get_or_create(
                athlete=athlete, text=text, defaults={"active": True}
            )
        return athlete

    def _ensure_pending_invite(self, coach):
        """A pending email invite (N4) so the roster's onboarding surface shows.

        ``open_for`` reuses the coach's open row on reseed (so duplicates don't
        pile up) and stamps a real TTL (N4 Phase 3), re-arming it if a prior run's
        invite has since aged out.
        """
        CoachInvite.open_for(coach=coach, email=PENDING_INVITE_EMAIL)

    def _ensure_pending_request(self, coach):
        """A pending athlete→coach request (N4 Phase 2) so the surface shows.

        An existing user (with no prior link to the coach) who has asked to
        train under them. ``update_or_create`` on the link keeps a reseed
        idempotent and repairs the row to ``pending_athlete_request`` if a prior
        run (or a manual accept/decline) left it elsewhere.
        """
        requester, created = User.objects.get_or_create(
            email=PENDING_REQUEST_EMAIL,
            defaults={"username": PENDING_REQUEST_EMAIL, "name": PENDING_REQUEST_NAME},
        )
        if created:
            requester.set_unusable_password()
            requester.save(update_fields=["password"])
        CoachAthlete.objects.update_or_create(
            coach=coach,
            athlete=requester,
            defaults={
                "status": CoachAthlete.Status.PENDING_ATHLETE_REQUEST,
                "invited_by": CoachAthlete.InvitedBy.ATHLETE,
                "responded_at": None,
                "ended_at": None,
            },
        )

    def _ensure_link(self, coach, athlete):
        """An active, coach-invited link (the prototype's roster is all-active).

        ``update_or_create`` so a reseed restores the demo link to ``active``
        even if a prior run (or a manual ``end()``) left it pending / declined /
        ended — otherwise the roster and ``Plan.objects.for_coach`` would keep
        excluding the athlete while the command reported success.
        """
        link, _ = CoachAthlete.objects.update_or_create(
            coach=coach,
            athlete=athlete,
            defaults={
                "status": CoachAthlete.Status.ACTIVE,
                "invited_by": CoachAthlete.InvitedBy.COACH,
                "responded_at": None,
                "ended_at": None,
            },
        )
        return link

    # -- the demo group ---------------------------------------------------

    def _ensure_group(self, coach):
        """A demo group with three of the athletes (idempotent).

        ``update_or_create`` restores the group to active on reseed; ``add_athlete``
        is idempotent and requires the active link the loop above already ensured.
        """
        group, _ = MesoGroup.objects.update_or_create(
            coach=coach,
            name=GROUP["name"],
            defaults={
                "focus": GROUP["focus"],
                "status": MesoGroup.Status.ACTIVE,
            },
        )
        email_to_slug = {s["email"]: s["slug"] for s in ATHLETES}
        emails = [s["email"] for s in ATHLETES if s["slug"] in GROUP["member_slugs"]]
        members = User.objects.filter(email__in=emails)
        memberships = {}
        for athlete in members:
            memberships[email_to_slug[athlete.email]] = group.add_athlete(athlete)
        # Groups Phase 2a: a shared program rooted at the group (created once, so
        # a reseed never spawns a second) — the group designer renders off it.
        if group.shared_plan() is None:
            group.create_shared_plan()
            self.stdout.write(f"  - built shared program for group '{group.name}'")
        self._ensure_group_overrides(group, memberships)
        self._ensure_group_delivery(group)
        self.stdout.write(
            f"  - ensured group '{group.name}' ({members.count()} members)"
        )
        return group

    def _ensure_group_overrides(self, group, memberships):
        """A couple of per-athlete auto-adjusts on the shared program (Phase 3).

        So a fresh DB renders the designer's ``adj`` badge off real diffs: two
        members adjust the first shared lift (a load % + a contraindication swap →
        a "2 adjusts" badge) and a third tweaks the second lift's volume. Idempotent
        — ``set_override`` upserts, so a reseed never piles up extra overrides.
        """
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

    def _ensure_group_delivery(self, group):
        """Deliver the group's shared current week to its members once (Phase 4).

        Idempotent: skipped once the shared week is stamped delivered, so a reseed
        never re-fans-out or piles up snapshots. Gives the three demo members a
        real, *resolved* delivered plan on their own athlete surface (Devon's load
        %, Priya's swap, Marcus's volume tweak all applied).
        """
        from store_project.meso.serializers import current_week

        plan = group.shared_plan()
        if plan is None:
            return
        week = current_week(plan)
        if week is None or week.delivered_at is not None:
            return
        group.deliver_current_week()
        self.stdout.write(f"  - delivered shared week to group '{group.name}' members")

    # -- the sample plan --------------------------------------------------

    def _ensure_plan(self, coach, athlete):
        link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
        # ``update_or_create`` restores the demo plan to ``active`` (and the
        # seeded goal/unit) on every run — a stale draft/archived plan would
        # otherwise be skipped by the bare designer/deliver redirect, which only
        # targets non-archived plans.
        plan, _ = Plan.objects.update_or_create(
            relationship=link,
            title=SAMPLE_PLAN["title"],
            defaults={
                "goal": SAMPLE_PLAN["goal"],
                "status": Plan.Status.ACTIVE,
                "unit": Unit.KILOGRAMS,
            },
        )
        if plan.mesocycles.exists():
            # Hierarchy already built — leave any coach edits to the demo grid
            # intact rather than clobbering them on reseed.
            self.stdout.write(f"  - sample plan '{plan.title}' present; ensured active")
            return plan

        for meso_spec in SAMPLE_PLAN["mesocycles"]:
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
        self.stdout.write(f"  - built sample plan '{plan.title}' for {athlete.name}")
        return plan

    # -- the sample logged session ----------------------------------------

    def _ensure_log(self, athlete, plan, today):
        """Deliver + log Maya's current-week "Lower" session (the first real log).

        Idempotent: the week is delivered once (the visibility gate the real
        logging flow requires), and the ``SessionLog`` + ``LoggedSet`` rows are
        created only if absent, so a reseed never duplicates or clobbers a hand-
        edited log. Returns None if the plan's hierarchy isn't present.
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
        if not created and log.sets.exists():
            self.stdout.write("  - sample logged session present; left intact")
        else:
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
            self.stdout.write(
                f"  - logged sample session '{session.name}' for {athlete.name}"
            )

        # Derive Maya's estimated 1RM from the logged session (the seed writes the
        # log directly, so the log endpoint's refresh hasn't run) — so the demo's
        # %1RM Box Squat shows a real 1RM in the designer + her logger. Idempotent.
        refresh_one_rms(athlete, list(prescriptions.values()), plan.unit)
        return log
