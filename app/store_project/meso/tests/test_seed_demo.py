"""Phase 5 — seed + retire mock.

``seed_meso_demo`` stands up the coach-side demo as real DB rows so a fresh dev
database renders the roster / profile / designer from actual data (no more
client-side fixtures). These tests cover the command's contract:

- it creates the coach (+ profile), the five athletes (+ profiles +
  contraindications), an active link each, and Maya's sample plan;
- the sample plan round-trips through ``serialize_plan`` to the designer's
  expected shape (3 sessions in the current week, a 4-week strip, a 4-block
  macrocycle with done/current/next/future states);
- it is idempotent (re-running never duplicates); and
- ``--delete`` tears the demo back down without touching the coach.
"""

import pytest
from django.contrib.auth.hashers import make_password
from django.core.management import call_command

from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachProfile
from store_project.meso.models import Contraindication
from store_project.meso.models import LoggedSet
from store_project.meso.models import Mesocycle
from store_project.meso.models import Plan
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.parsing import parse_prescription
from store_project.meso.presenters import session_results
from store_project.meso.serializers import serialize_plan
from store_project.users.models import User

pytestmark = pytest.mark.django_db

COACH_EMAIL = "coach@example.test"
ATHLETE_EMAILS = [
    "maya.okonkwo@example.com",
    "devon.reyes@example.com",
    "priya.nair@example.com",
    "marcus.tan@example.com",
    "lena.kovic@example.com",
]


def seed(**options):
    call_command("seed_meso_demo", coach_email=COACH_EMAIL, **options)


class TestSeedCreatesDemo:
    def test_creates_coach_with_profile(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        profile = CoachProfile.objects.get(user=coach)
        assert "Compound-first" in profile.programming_style
        assert profile.avoid_rules  # the coach's avoid-rules are set

    def test_creates_five_athletes_with_profiles(self):
        seed()
        athletes = User.objects.filter(email__in=ATHLETE_EMAILS)
        assert athletes.count() == 5
        assert AthleteProfile.objects.filter(user__in=athletes).count() == 5

    def test_creates_global_contraindications(self):
        seed()
        maya = User.objects.get(email="maya.okonkwo@example.com")
        priya = User.objects.get(email="priya.nair@example.com")
        assert maya.contraindications.count() == 2
        assert priya.contraindications.count() == 0

    def test_creates_active_links(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        links = CoachAthlete.objects.for_coach(coach).active()
        assert links.count() == 5

    def test_creates_a_pending_invite(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        pending = CoachInvite.objects.for_coach(coach).pending()
        assert pending.count() == 1
        assert pending.get().email == "prospect@example.com"

    def test_reseed_does_not_duplicate_pending_invite(self):
        seed()
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        assert CoachInvite.objects.for_coach(coach).pending().count() == 1

    def test_delete_removes_pending_invite(self):
        seed()
        seed(delete=True)
        assert not CoachInvite.objects.filter(email="prospect@example.com").exists()

    def test_creates_a_pending_request(self):
        # N4 Phase 2: an athlete who has asked to train under the coach, so the
        # roster's pending-request surface is visible on a fresh DB.
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        pending = CoachAthlete.objects.for_coach(coach).filter(
            status=CoachAthlete.Status.PENDING_ATHLETE_REQUEST
        )
        assert pending.count() == 1
        assert pending.get().athlete.email == "hopeful@example.com"

    def test_reseed_does_not_duplicate_pending_request(self):
        seed()
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        assert (
            CoachAthlete.objects.for_coach(coach)
            .filter(status=CoachAthlete.Status.PENDING_ATHLETE_REQUEST)
            .count()
            == 1
        )

    def test_delete_removes_pending_request(self):
        seed()
        seed(delete=True)
        assert not User.objects.filter(email="hopeful@example.com").exists()

    def test_creates_a_past_athlete(self):
        # The relationship-history surface ("Past athletes"): a former athlete on
        # an ENDED link, so the surface is populated on a fresh DB.
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        past = CoachAthlete.objects.for_coach(coach).closed()
        assert past.count() == 1
        link = past.get()
        assert link.status == CoachAthlete.Status.ENDED
        assert link.athlete.email == "alum@example.com"
        assert link.ended_at is not None

    def test_reseed_does_not_duplicate_past_athlete(self):
        seed()
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        assert CoachAthlete.objects.for_coach(coach).closed().count() == 1

    def test_delete_removes_past_athlete(self):
        seed()
        seed(delete=True)
        assert not User.objects.filter(email="alum@example.com").exists()

    def test_creates_one_sample_plan(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plans = Plan.objects.for_coach(coach)
        assert plans.count() == 1
        plan = plans.get()
        assert plan.athlete.email == "maya.okonkwo@example.com"
        assert plan.status == Plan.Status.ACTIVE

    def test_sample_plan_hierarchy(self):
        seed()
        plan = Plan.objects.for_coach(User.objects.get(email=COACH_EMAIL)).get()
        assert plan.mesocycles.count() == 4
        hypertrophy = plan.mesocycles.get(name="Hypertrophy")
        assert hypertrophy.weeks.count() == 4
        current = hypertrophy.weeks.get(is_current=True)
        assert current.index == 2
        assert hypertrophy.weeks.get(is_deload=True).index == 4
        # The fixed lineup is DENSE across the whole block (P0 invariant: every
        # slot × live-week has a line-0 cell) — every week materializes the same
        # 3 days, so no live week is a half-built shell.
        assert current.sessions.count() == 3
        expected_cells = current.cells.filter(line=0).count()
        assert expected_cells == 15  # 3 days × 5 rows
        assert Session.objects.filter(week__mesocycle=hypertrophy).count() == 12
        for week in hypertrophy.weeks.all():
            assert week.sessions.count() == 3
            assert week.cells.filter(line=0).count() == expected_cells
        # The current week's one freeform sub-line: the Hanging Knee Raise
        # substitution typed as text (§2.6), not a swap field.
        assert list(
            current.cells.filter(line__gte=1).values_list("text", flat=True)
        ) == ["Cable Crunch"]


class TestSamplePlanRoundTrips:
    def test_serializes_to_designer_shape(self):
        seed()
        plan = Plan.objects.for_coach(User.objects.get(email=COACH_EMAIL)).get()
        data = serialize_plan(plan)

        assert data["plan"]["title"] == "Hypertrophy Block"
        assert len(data["program"]) == 3  # 3 sessions in the current week
        assert [s["name"] for s in data["program"]] == ["Lower", "Upper", "Posterior"]
        assert len(data["weeks"]) == 4  # the mesocycle's week strip
        assert [p["state"] for p in data["phases"]] == [
            "done",
            "current",
            "next",
            "future",
        ]

    def test_knee_safe_tag_round_trips(self):
        seed()
        plan = Plan.objects.for_coach(User.objects.get(email=COACH_EMAIL)).get()
        data = serialize_plan(plan)
        lower = next(s for s in data["program"] if s["name"] == "Lower")
        box_squat = lower["exercises"][0]
        assert box_squat["name"] == "Box Squat (to parallel)"
        assert box_squat["tag"] == "knee-safe"

    def test_percent_1rm_prescription_round_trips(self):
        # The demo squat's cell text carries a % of 1RM load token (S2), shown
        # once on a fresh DB and not duplicated on reseed.
        seed()
        seed()  # reseed must not spawn a second %1RM row
        plan = Plan.objects.for_coach(User.objects.get(email=COACH_EMAIL)).get()
        data = serialize_plan(plan)
        lower = next(s for s in data["program"] if s["name"] == "Lower")
        box_squat = lower["exercises"][0]
        assert parse_prescription(box_squat["text"])["load"] == "72%"
        percent_rows = [
            ex
            for session in data["program"]
            for ex in session["exercises"]
            if ((parse_prescription(ex["text"]) or {}).get("load") or "").endswith("%")
        ]
        assert len(percent_rows) == 1


class TestSeedLogsASession:
    """Maya's current-week "Lower" session is delivered + logged (Phase 3).

    The demo's first real logged rows — so the coach's results screen and the
    designer's "last time" column render off actual data, not fixtures.
    """

    def _lower_session(self):
        coach = User.objects.get(email=COACH_EMAIL)
        plan = Plan.objects.for_coach(coach).get()
        return Session.objects.get(
            week__mesocycle__plan=plan,
            week__mesocycle__name="Hypertrophy",
            week__index=2,
            session_slot__day_number=1,
        )

    def test_delivers_and_logs_the_lower_session(self):
        seed()
        session = self._lower_session()
        assert session.week.delivered_at is not None  # visibility gate stamped
        maya = User.objects.get(email="maya.okonkwo@example.com")
        log = SessionLog.objects.get(session=session, athlete=maya)
        assert log.status == SessionLog.Status.DONE
        assert log.sets.count() == 17  # 4 + 3 + 3 + 3 + 4

    def test_log_drives_the_results_screen(self):
        seed()
        ctx = session_results(self._lower_session())
        assert ctx["summary"]["logged_state"] is True
        assert ctx["summary"]["completion"] == 100
        # Box Squat's top set ran hot → a real flag the coach can act on.
        assert ctx["summary"]["flag_count"] == 1
        assert "Box Squat" in ctx["summary"]["flag"]
        # The leg curl's last set fell short on reps (12, 12, 9 vs 3×12).
        rows = {r["name"]: r for r in ctx["rows"]}
        assert rows["Seated Leg Curl"]["note"] == "missed 3 reps on set 3"

    def test_log_lights_the_designer_last_column(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plan = Plan.objects.for_coach(coach).get()
        lower = next(s for s in serialize_plan(plan)["program"] if s["name"] == "Lower")
        box_squat = lower["exercises"][0]
        assert box_squat["last"] == "4×6 · 70kg · RPE8.5"

    def test_log_derives_a_one_rm_for_the_percent_row(self):
        # Box Squat is a %1RM row; the seed derives Maya's 1RM from her logged
        # session (Epley of 70 × 6 = 84) so the coach sees it on the designer row.
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plan = Plan.objects.for_coach(coach).get()
        lower = next(s for s in serialize_plan(plan)["program"] if s["name"] == "Lower")
        assert lower["exercises"][0]["one_rm"] == "84"

    def test_reseed_does_not_duplicate_the_log(self):
        seed()
        seed()
        session = self._lower_session()
        maya = User.objects.get(email="maya.okonkwo@example.com")
        assert SessionLog.objects.filter(session=session, athlete=maya).count() == 1
        assert LoggedSet.objects.filter(session_log__session=session).count() == 17


class TestIdempotent:
    def test_rerun_does_not_duplicate(self):
        seed()
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        assert User.objects.filter(email__in=ATHLETE_EMAILS).count() == 5
        # 5 active athletes + 1 pending athlete→coach request (N4 Phase 2) + 1
        # ended past athlete (the relationship-history surface).
        assert CoachAthlete.objects.for_coach(coach).active().count() == 5
        assert CoachAthlete.objects.for_coach(coach).count() == 7
        assert Plan.objects.for_coach(coach).count() == 1
        # Children are not re-created on a second run (the individual sample plan).
        assert (
            Mesocycle.objects.filter(plan__in=Plan.objects.for_coach(coach)).count()
            == 4
        )
        maya = User.objects.get(email="maya.okonkwo@example.com")
        assert maya.contraindications.count() == 2

    def test_rerun_preserves_existing_coach_password(self):
        existing = User.objects.create(
            email=COACH_EMAIL,
            username=COACH_EMAIL,
            password=make_password("original-secret"),
        )
        seed()
        existing.refresh_from_db()
        assert existing.check_password("original-secret")


class TestReseedReconciles:
    """A reseed restores the demo to a working state, not just creates-if-absent.

    The demo's whole point is an *active* roster + a non-archived plan the bare
    designer/deliver redirect can target; a rerun must repair a link/plan a prior
    run (or a manual end/archive) left in some other state.
    """

    def test_reactivates_an_ended_link(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        maya = User.objects.get(email="maya.okonkwo@example.com")
        link = CoachAthlete.objects.get(coach=coach, athlete=maya)
        link.end()  # ended → also archives the plan
        assert not link.is_active

        seed()
        link.refresh_from_db()
        assert link.is_active

    def test_restores_an_archived_sample_plan(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plan = Plan.objects.for_coach(coach).get()
        plan.status = Plan.Status.ARCHIVED
        plan.save(update_fields=["status"])

        seed()
        plan.refresh_from_db()
        assert plan.status == Plan.Status.ACTIVE
        # back to being the bare designer/deliver redirect target
        assert plan in Plan.objects.for_coach(coach)

    def test_rebuilds_a_plan_missing_its_hierarchy(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plan = Plan.objects.for_coach(coach).get()  # the individual sample plan
        plan.mesocycles.all().delete()  # stale plan row with no children
        assert plan.mesocycles.count() == 0

        seed()
        assert plan.mesocycles.count() == 4


class TestDelete:
    def test_delete_removes_demo_but_keeps_coach(self):
        seed()
        seed(delete=True)
        coach = User.objects.get(email=COACH_EMAIL)  # coach survives
        assert User.objects.filter(email__in=ATHLETE_EMAILS).count() == 0
        assert CoachAthlete.objects.for_coach(coach).count() == 0
        assert Plan.objects.for_coach(coach).count() == 0
        assert Contraindication.objects.count() == 0
