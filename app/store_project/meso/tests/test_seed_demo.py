"""Phase 5 — seed + retire mock.

``seed_meso_demo`` stands up the coach-side demo as real DB rows so a fresh dev
database renders the roster / profile / designer from actual data (no more
client-side fixtures). These tests cover the command's contract:

- it creates the coach (+ profile), the five athletes (+ profiles +
  contraindications), an active link each, and full programs for three of
  them (Maya / Devon / Priya — Marcus and Lena stay plan-less);
- Maya's sample plan round-trips through ``serialize_plan`` to the designer's
  expected shape (3 sessions in a week, a 4-week strip, a 4-block macrocycle
  with done/current/next/future states) — her Hypertrophy block's
  logged-through week (index 2) is preserved verbatim (the original fixture
  grid);
- Devon's and Priya's plans are each fully built (every block, every week),
  every live week delivered (2d: delivery no longer gates visibility), with a
  logged multi-week history before a seed-data-only "logged-through" cutoff;
- it is idempotent (re-running never duplicates); and
- ``--delete`` tears the demo back down without touching the coach.
"""

import pytest
from django.contrib.auth.hashers import make_password
from django.core.management import call_command

from store_project.meso.management.commands.seed_meso_demo import _ease_rpe
from store_project.meso.management.commands.seed_meso_demo import _week_cell
from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachProfile
from store_project.meso.models import Contraindication
from store_project.meso.models import LoggedSet
from store_project.meso.models import Mesocycle
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.models import Week
from store_project.meso.parsing import parse_prescription
from store_project.meso.presenters import session_results
from store_project.meso.serializers import serialize_plan
from store_project.users.models import User

pytestmark = pytest.mark.django_db

COACH_EMAIL = "coach@example.test"
MAYA_EMAIL = "maya.okonkwo@example.com"
DEVON_EMAIL = "devon.reyes@example.com"
PRIYA_EMAIL = "priya.nair@example.com"
ATHLETE_EMAILS = [
    MAYA_EMAIL,
    DEVON_EMAIL,
    PRIYA_EMAIL,
    "marcus.tan@example.com",
    "lena.kovic@example.com",
]


def seed(**options):
    call_command("seed_meso_demo", coach_email=COACH_EMAIL, **options)


def _plan_for(coach, athlete_email):
    """The one plan ``seed_meso_demo`` built for this athlete (of the coach's 3)."""
    return Plan.objects.for_coach(coach).get(relationship__athlete__email=athlete_email)


def _maya_logged_through_week(plan):
    """Maya's Hypertrophy-block logged-through week (index 2).

    The hand-authored fixture the round-trip tests pin exact cell text
    against. ``current_week(plan)`` (called by ``serialize_plan(plan)`` with
    no explicit ``week``) now defaults to the plan's EARLIEST live week
    overall — Base/GPP week 1, a different block entirely (docs/meso/remove-
    current-week-plan.md) — so callers that care about the Hypertrophy
    fixture specifically must pass this week in explicitly rather than
    relying on the bare default.
    """
    hypertrophy = plan.mesocycles.get(name="Hypertrophy")
    return hypertrophy.weeks.get(index=2)


class TestWeekCellSplitsRpeOntoItsOwnLine:
    """The seeder's cell shape is a vertical stack, not one crammed line.

    The repo owner's real cells put sets×reps (+ load) on line 0 and RPE on
    its own sub-line (line 1) — see the module docstring / spreadsheet-
    parity-plan §2.1, §2.6. ``_week_cell`` composes line 0 the same way as
    before, minus RPE, and (when the scheme carries an RPE) adds a ``"lines"``
    sub-line matching ``compose_prescription_text``'s own RPE formatting.
    """

    def test_cell_with_rpe_has_no_rpe_on_line_0_and_rpe_on_line_1(self):
        scheme = {"sets": 3, "reps": 12, "rpe": 6.5, "load": 100, "load_step": 0}
        cell = _week_cell(scheme, week_index=1)
        assert "RPE" not in cell["text"]
        assert cell["text"] == "3 x 12, 100"
        assert cell["lines"] == ["RPE 6.5"]

    def test_cell_with_no_rpe_has_no_sub_line(self):
        # The accessory rows (``rpe=None``) must not gain a blank sub-line.
        scheme = {"sets": 4, "reps": 15, "rpe": None, "load": 55, "load_step": 0}
        cell = _week_cell(scheme, week_index=1)
        assert cell["text"] == "4 x 15, 55"
        assert "lines" not in cell

    def test_deload_week_still_puts_the_eased_rpe_on_line_1(self):
        scheme = {"sets": 4, "reps": 9, "rpe": 7.5, "load": 100, "load_step": 0}
        cell = _week_cell(scheme, week_index=4, deload_index=4)
        assert cell["lines"] == [f"RPE {_ease_rpe(7.5)}"]
        assert cell["lines"] == ["RPE 6"]
        assert "RPE" not in cell["text"]

    def test_blank_scheme_still_yields_a_blank_cell(self):
        assert _week_cell(None, week_index=1) == {}
        assert _week_cell({}, week_index=1) == {}


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

    def test_creates_three_sample_plans(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plans = Plan.objects.for_coach(coach)
        assert plans.count() == 3
        athlete_emails = {plan.athlete.email for plan in plans}
        assert athlete_emails == {MAYA_EMAIL, DEVON_EMAIL, PRIYA_EMAIL}
        assert all(plan.status == Plan.Status.ACTIVE for plan in plans)
        # Marcus and Lena stay plan-less.
        assert (
            not Plan.objects.for_coach(coach)
            .filter(
                relationship__athlete__email__in=[
                    "marcus.tan@example.com",
                    "lena.kovic@example.com",
                ]
            )
            .exists()
        )

    def test_sample_plan_hierarchy(self):
        seed()
        plan = _plan_for(User.objects.get(email=COACH_EMAIL), MAYA_EMAIL)
        assert plan.mesocycles.count() == 4
        hypertrophy = plan.mesocycles.get(name="Hypertrophy")
        assert hypertrophy.weeks.count() == 4
        # The demo's logged-through week (a seed-data-only marker, not a
        # materialized field — see seed_meso_demo.py's ``logged_through_index``).
        logged_through = hypertrophy.weeks.get(index=2)
        assert hypertrophy.weeks.get(is_deload=True).index == 4
        # The fixed lineup is DENSE across the whole block (P0 invariant: every
        # slot × live-week has a line-0 cell) — every week materializes the same
        # 3 days, so no live week is a half-built shell.
        assert logged_through.sessions.count() == 3
        expected_cells = logged_through.cells.filter(line=0).count()
        assert expected_cells == 15  # 3 days × 5 rows
        assert Session.objects.filter(week__mesocycle=hypertrophy).count() == 12
        for week in hypertrophy.weeks.all():
            assert week.sessions.count() == 3
            assert week.cells.filter(line=0).count() == expected_cells
        # The logged-through week's one freeform sub-line: the Hanging Knee
        # Raise substitution typed as text (§2.6), not a swap field.
        assert list(
            logged_through.cells.filter(line__gte=1).values_list("text", flat=True)
        ) == ["Cable Crunch"]

    def test_generated_weeks_carry_rpe_as_a_line_1_sub_line(self):
        # Week 1 of Hypertrophy is generator-built (unlike week 2's
        # hand-authored fixture above), so this exercises ``_week_cell``'s
        # real output end to end: the seeder runs without error and produces
        # real ``Prescription`` rows at both line 0 (sets×reps, no RPE) and
        # line 1 (the RPE sub-line) for every row whose scheme carries an
        # RPE — and no line-1 row at all for the accessory rows that don't.
        seed()
        plan = _plan_for(User.objects.get(email=COACH_EMAIL), MAYA_EMAIL)
        hypertrophy = plan.mesocycles.get(name="Hypertrophy")
        week1 = hypertrophy.weeks.get(index=1)
        lower_day = week1.sessions.get(session_slot__day_number=1).session_slot
        rows = list(lower_day.exercise_slots.order_by("order"))
        by_name = {row.name: row for row in rows}

        box_squat = by_name["Box Squat (to parallel)"]
        line0 = Prescription.objects.get(exercise_slot=box_squat, week=week1, line=0)
        line1 = Prescription.objects.get(exercise_slot=box_squat, week=week1, line=1)
        assert "RPE" not in line0.text
        assert line1.text == "RPE 7"

        # "Standing Calf Raise" is seeded with ``rpe=None`` (an accessory row)
        # — it must not gain a blank/absent sub-line row at all.
        calf_raise = by_name["Standing Calf Raise"]
        calf_line0 = Prescription.objects.get(
            exercise_slot=calf_raise, week=week1, line=0
        )
        assert "RPE" not in calf_line0.text
        assert not Prescription.objects.filter(
            exercise_slot=calf_raise, week=week1, line__gte=1
        ).exists()


class TestSamplePlanRoundTrips:
    def test_serializes_to_designer_shape(self):
        seed()
        plan = _plan_for(User.objects.get(email=COACH_EMAIL), MAYA_EMAIL)
        week = _maya_logged_through_week(plan)
        data = serialize_plan(plan, week=week)

        assert data["plan"]["title"] == "Hypertrophy Block"
        assert len(data["program"]) == 3  # 3 sessions in this week
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
        plan = _plan_for(User.objects.get(email=COACH_EMAIL), MAYA_EMAIL)
        week = _maya_logged_through_week(plan)
        data = serialize_plan(plan, week=week)
        lower = next(s for s in data["program"] if s["name"] == "Lower")
        box_squat = lower["exercises"][0]
        assert box_squat["name"] == "Box Squat (to parallel)"
        assert box_squat["tag"] == "knee-safe"

    def test_percent_1rm_prescription_round_trips(self):
        # The demo squat's cell text carries a % of 1RM load token (S2), shown
        # once on a fresh DB and not duplicated on reseed.
        seed()
        seed()  # reseed must not spawn a second %1RM row
        plan = _plan_for(User.objects.get(email=COACH_EMAIL), MAYA_EMAIL)
        week = _maya_logged_through_week(plan)
        data = serialize_plan(plan, week=week)
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
        plan = _plan_for(coach, MAYA_EMAIL)
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
        maya = User.objects.get(email=MAYA_EMAIL)
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
        plan = _plan_for(coach, MAYA_EMAIL)
        week = _maya_logged_through_week(plan)
        lower = next(
            s
            for s in serialize_plan(plan, week=week)["program"]
            if s["name"] == "Lower"
        )
        box_squat = lower["exercises"][0]
        assert box_squat["last"] == "4×6 · 70kg · RPE8.5"

    def test_log_derives_a_one_rm_for_the_percent_row(self):
        # Box Squat is a %1RM row; the seed derives Maya's 1RM from her logged
        # session (Epley of 70 × 6 = 84) so the coach sees it on the designer row.
        # Her historical Hypertrophy wk1 log (also a %1RM row) and her separately
        # named Base/GPP-block history don't contribute a competing estimate.
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plan = _plan_for(coach, MAYA_EMAIL)
        week = _maya_logged_through_week(plan)
        lower = next(
            s
            for s in serialize_plan(plan, week=week)["program"]
            if s["name"] == "Lower"
        )
        assert lower["exercises"][0]["one_rm"] == "84"

    def test_reseed_does_not_duplicate_the_log(self):
        seed()
        seed()
        session = self._lower_session()
        maya = User.objects.get(email=MAYA_EMAIL)
        assert SessionLog.objects.filter(session=session, athlete=maya).count() == 1
        assert LoggedSet.objects.filter(session_log__session=session).count() == 17


class TestDevonAndPriyaPrograms:
    """Devon and Priya each get a full program too (parity with Maya's).

    Every block (Base/GPP → Hypertrophy → Strength → Peak/Test) is built with
    a fixed lineup and real per-week prescription text in every week. 2d:
    delivery no longer gates visibility, so every live week is simply
    delivered — there's no "future, undelivered" state left to model. A
    logged-through cutoff still exists purely as seed-data bookkeeping (see
    seed_meso_demo.py's ``logged_through_index``, never a materialized
    field): every week strictly before it gets a real multi-week logged
    history; nothing at or after it is logged.
    """

    @pytest.mark.parametrize(
        "email", [DEVON_EMAIL, PRIYA_EMAIL], ids=["devon", "priya"]
    )
    def test_plan_is_active_with_all_blocks_built(self, email):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plan = _plan_for(coach, email)
        assert plan.status == Plan.Status.ACTIVE
        assert plan.mesocycles.count() == 4
        for mesocycle in plan.mesocycles.all():
            weeks = list(mesocycle.weeks.all())
            assert weeks  # every block is fully built, not planned-length-only
            for week in weeks:
                assert week.sessions.exists()
                # Every row of every day has real (non-blank) prescription text.
                assert all(
                    cell.text.strip()
                    for cell in week.cells.filter(line=0)
                    if not cell.skipped
                )

    @pytest.mark.parametrize(
        "email", [DEVON_EMAIL, PRIYA_EMAIL], ids=["devon", "priya"]
    )
    def test_every_live_week_is_delivered(self, email):
        # 2d + the seed decision (docs/meso/remove-current-week-plan.md §6):
        # delivery no longer gates visibility, so there's no "future,
        # undelivered" distinction left to model — the seed simply delivers
        # every live week.
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plan = _plan_for(coach, email)
        weeks = Week.objects.filter(mesocycle__plan=plan)
        assert weeks.exists()
        assert all(w.delivered_at is not None for w in weeks)

    @pytest.mark.parametrize(
        "email", [DEVON_EMAIL, PRIYA_EMAIL], ids=["devon", "priya"]
    )
    def test_weeks_before_the_logged_through_cutoff_have_a_real_history(self, email):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        athlete = User.objects.get(email=email)
        plan = _plan_for(coach, email)
        weeks = list(
            Week.objects.filter(mesocycle__plan=plan)
            .select_related("mesocycle")
            .order_by("mesocycle__order", "index")
        )
        logged_flags = [
            SessionLog.objects.filter(
                session__week=w, athlete=athlete, status=SessionLog.Status.DONE
            ).exists()
            for w in weeks
        ]
        assert any(logged_flags)  # meaningful history exists
        assert not all(logged_flags)  # a future, unlogged tail exists too
        # Logging is contiguous from the start of the plan (the seed's
        # ``logged_through_index`` cutoff, a plain seed-data marker — see
        # seed_meso_demo.py's ``_log_plan_history`` — never a materialized
        # field): every logged week precedes every unlogged one.
        last_logged = max(i for i, logged in enumerate(logged_flags) if logged)
        assert all(logged_flags[: last_logged + 1])
        assert not any(logged_flags[last_logged + 1 :])
        for week in weeks[: last_logged + 1]:
            logs = SessionLog.objects.filter(
                session__week=week, athlete=athlete, status=SessionLog.Status.DONE
            )
            assert LoggedSet.objects.filter(session_log__in=logs).count() > 0
        for week in weeks[last_logged + 1 :]:
            assert not SessionLog.objects.filter(
                session__week=week, athlete=athlete
            ).exists()

    @pytest.mark.parametrize(
        "email", [DEVON_EMAIL, PRIYA_EMAIL], ids=["devon", "priya"]
    )
    def test_reseed_does_not_duplicate_the_history(self, email):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plan = _plan_for(coach, email)
        before_counts = (
            SessionLog.objects.filter(session__week__mesocycle__plan=plan).count(),
            LoggedSet.objects.filter(
                session_log__session__week__mesocycle__plan=plan
            ).count(),
        )
        assert before_counts[0] > 0  # history was actually created

        seed()
        after_counts = (
            SessionLog.objects.filter(session__week__mesocycle__plan=plan).count(),
            LoggedSet.objects.filter(
                session_log__session__week__mesocycle__plan=plan
            ).count(),
        )
        assert after_counts == before_counts


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
        assert Plan.objects.for_coach(coach).count() == 3
        # Children are not re-created on a second run (3 clients × 4 blocks).
        assert (
            Mesocycle.objects.filter(plan__in=Plan.objects.for_coach(coach)).count()
            == 12
        )
        maya = User.objects.get(email=MAYA_EMAIL)
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
        maya = User.objects.get(email=MAYA_EMAIL)
        link = CoachAthlete.objects.get(coach=coach, athlete=maya)
        link.end()  # ended → also archives the plan
        assert not link.is_active

        seed()
        link.refresh_from_db()
        assert link.is_active

    def test_restores_an_archived_sample_plan(self):
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plan = _plan_for(coach, MAYA_EMAIL)
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
        plan = _plan_for(coach, MAYA_EMAIL)  # the individual sample plan
        plan.mesocycles.all().delete()  # stale plan row with no children
        assert plan.mesocycles.count() == 0

        seed()
        assert plan.mesocycles.count() == 4

    def test_rebuilds_a_partially_built_plan(self):
        # A DB seeded by an earlier version of the command left every block but
        # Hypertrophy planned-length-only (mesocycle rows, no Week/Session/
        # Prescription). A bare ``mesocycles.exists()`` guard would skip the
        # upgrade and strand that partial shape; the reseed must top it up so
        # every block is materialized.
        seed()
        coach = User.objects.get(email=COACH_EMAIL)
        plan = _plan_for(coach, MAYA_EMAIL)
        # Mimic the old partial shape: drop the weeks of every non-Hypertrophy
        # block (leaving the mesocycle rows in place).
        stale = plan.mesocycles.exclude(name="Hypertrophy")
        Week.objects.filter(mesocycle__in=stale).delete()
        assert not Week.objects.filter(mesocycle__in=stale).exists()

        seed()
        for mesocycle in plan.mesocycles.all():
            assert mesocycle.weeks.exists()  # every block materialized again
            for week in mesocycle.weeks.all():
                assert week.sessions.exists()


class TestDelete:
    def test_delete_removes_demo_but_keeps_coach(self):
        seed()
        seed(delete=True)
        coach = User.objects.get(email=COACH_EMAIL)  # coach survives
        assert User.objects.filter(email__in=ATHLETE_EMAILS).count() == 0
        assert CoachAthlete.objects.for_coach(coach).count() == 0
        assert Plan.objects.for_coach(coach).count() == 0
        assert Contraindication.objects.count() == 0

    def test_delete_removes_all_three_clients_plans_and_logs(self):
        seed()
        # Sanity: all three clients actually got a plan + logged history first.
        assert (
            Plan.objects.filter(
                relationship__athlete__email__in=[MAYA_EMAIL, DEVON_EMAIL, PRIYA_EMAIL]
            ).count()
            == 3
        )
        assert (
            SessionLog.objects.filter(
                athlete__email__in=[MAYA_EMAIL, DEVON_EMAIL, PRIYA_EMAIL]
            ).count()
            > 0
        )

        seed(delete=True)
        assert not Plan.objects.filter(
            relationship__athlete__email__in=[MAYA_EMAIL, DEVON_EMAIL, PRIYA_EMAIL]
        ).exists()
        assert not Mesocycle.objects.filter(
            plan__relationship__athlete__email__in=[
                MAYA_EMAIL,
                DEVON_EMAIL,
                PRIYA_EMAIL,
            ]
        ).exists()
        assert not SessionLog.objects.filter(
            athlete__email__in=[MAYA_EMAIL, DEVON_EMAIL, PRIYA_EMAIL]
        ).exists()
        assert not LoggedSet.objects.exists()
