"""Athlete slice Phase 1 — the athlete's read surface.

The first logged-in surface for an *athlete* (distinct from the coach's view of
an athlete at ``/meso/athlete/<uuid>/``):

- ``/meso/me/`` lists the athlete's active-coach plans, each with its latest
  delivered week and that week's sessions (marked done/pending from the
  athlete's own ``SessionLog``);
- ``/meso/me/session/<id>/`` shows one delivered session's prescribed exercises,
  read-only.

These tests pin the **scoping contract** (see ``docs/archive/meso/athlete-plan.md``):
an athlete sees only plans across their *active* coaches, only **delivered**
weeks, and never another athlete's data. Delivery — not plan status — is the
publish gate; an undelivered week is invisible even to its own athlete.
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.models import SessionSlot
from store_project.meso.models import Week
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc as make_presc
from store_project.meso.tests._helpers import sub_line
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed(
    *,
    coach=None,
    athlete=None,
    delivered=True,
    link_status=CoachAthlete.Status.ACTIVE,
    plan_status=Plan.Status.ACTIVE,
    session_name="Lower",
):
    """A minimal plan → (optionally delivered) week → session → prescription."""
    coach = coach or UserFactory()
    athlete = athlete or UserFactory()
    rel = CoachAthleteFactory(coach=coach, athlete=athlete, status=link_status)
    plan = PlanFactory(relationship=rel, title="Hypertrophy Block", status=plan_status)
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(
        mesocycle=meso,
        index=2,
        is_current=True,
        delivered_at=timezone.now() if delivered else None,
    )
    session = day(week, day_number=1, name=session_name, bias="Quad")
    presc = make_presc(
        session, name="Box Squat", sets="4", reps="6", load="70", rpe="7"
    )
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        rel=rel,
        plan=plan,
        meso=meso,
        week=week,
        session=session,
        presc=presc,
    )


def seed_block(
    *,
    coach=None,
    athlete=None,
    skip_first=False,
    sub_second="",
    third_delivered=False,
):
    """A three-week block of ONE day × ONE exercise row (P3 athlete multi-week).

    All three weeks share one ``SessionSlot`` (the day) and one ``ExerciseSlot``
    (the row), with a per-week ``Prescription`` cell carrying a distinct load so
    each week's column is identifiable. Weeks 1 & 2 are delivered (the whole
    block delivers at once); week 2 is the athlete's ``is_current`` week; week 3
    is left undelivered unless ``third_delivered`` (a week the coach is still
    building — invisible to the athlete). ``sub_second`` adds a freeform
    sub-line (e.g. a substitution) beneath week 2's cell.
    """
    coach = coach or UserFactory()
    athlete = athlete or UserFactory()
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    now = timezone.now()
    slot = SessionSlot.objects.create(
        mesocycle=meso, day_number=1, name="Lower", bias="Quad", order=0
    )
    ex = ExerciseSlot.objects.create(session_slot=slot, name="Box Squat", order=0)
    w1 = WeekFactory(mesocycle=meso, index=1, is_current=False, delivered_at=now)
    w2 = WeekFactory(mesocycle=meso, index=2, is_current=True, delivered_at=now)
    w3 = WeekFactory(
        mesocycle=meso,
        index=3,
        is_current=False,
        delivered_at=now if third_delivered else None,
    )
    s1 = day(w1, session_slot=slot)
    s2 = day(w2, session_slot=slot)
    s3 = day(w3, session_slot=slot)
    c1 = make_presc(
        exercise_slot=ex,
        week=w1,
        sets="4",
        reps="8",
        load="71",
        rpe="7",
        skipped=skip_first,
    )
    c2 = make_presc(exercise_slot=ex, week=w2, sets="4", reps="8", load="101", rpe="8")
    if sub_second:
        sub_line(c2, sub_second)
    c3 = make_presc(exercise_slot=ex, week=w3, sets="4", reps="8", load="131", rpe="8")
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        plan=plan,
        meso=meso,
        slot=slot,
        ex=ex,
        w1=w1,
        w2=w2,
        w3=w3,
        s1=s1,
        s2=s2,
        s3=s3,
        c1=c1,
        c2=c2,
        c3=c3,
    )


def seed_two_block(*, coach=None, athlete=None):
    """A two-mesocycle plan (issue #456 Finding 1): both blocks delivered.

    Block A ("Base", order 0) carries two delivered weeks, week 2 of which is
    the athlete's ``is_current`` week — the athlete is mid-block-A. Block B
    ("Peak", order 1) carries one delivered week, index 1, ``is_current``
    False — a coach delivering a brand-new mesocycle never moves the pointer
    (by design), so block B starts out reachable only via plan-wide
    navigation, never the anchored block A alone.
    """
    coach = coach or UserFactory()
    athlete = athlete or UserFactory()
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(
        relationship=rel, title="Two-Block Plan", status=Plan.Status.ACTIVE
    )
    now = timezone.now()

    meso_a = MesocycleFactory(plan=plan, name="Base", order=0)
    slot_a = SessionSlot.objects.create(
        mesocycle=meso_a, day_number=1, name="Squat Day", bias="Quad", order=0
    )
    ex_a = ExerciseSlot.objects.create(session_slot=slot_a, name="Back Squat", order=0)
    a1 = WeekFactory(mesocycle=meso_a, index=1, is_current=False, delivered_at=now)
    a2 = WeekFactory(mesocycle=meso_a, index=2, is_current=True, delivered_at=now)
    sa1 = day(a1, session_slot=slot_a)
    sa2 = day(a2, session_slot=slot_a)
    make_presc(exercise_slot=ex_a, week=a1, sets="5", reps="5", load="90", rpe="8")
    make_presc(exercise_slot=ex_a, week=a2, sets="5", reps="5", load="100", rpe="8")

    meso_b = MesocycleFactory(plan=plan, name="Peak", order=1)
    slot_b = SessionSlot.objects.create(
        mesocycle=meso_b, day_number=1, name="Bench Day", bias="Push", order=0
    )
    ex_b = ExerciseSlot.objects.create(session_slot=slot_b, name="Bench Press", order=0)
    b1 = WeekFactory(mesocycle=meso_b, index=1, is_current=False, delivered_at=now)
    sb1 = day(b1, session_slot=slot_b)
    make_presc(exercise_slot=ex_b, week=b1, sets="3", reps="5", load="80", rpe="8")

    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        rel=rel,
        plan=plan,
        meso_a=meso_a,
        meso_b=meso_b,
        a1=a1,
        a2=a2,
        b1=b1,
        sa1=sa1,
        sa2=sa2,
        sb1=sb1,
    )


HOME = reverse("meso:athlete_home")


def session_url(session):
    return reverse("meso:athlete_session", kwargs={"pk": session.pk})


# -- athlete home ----------------------------------------------------------


class TestAthleteHome:
    def test_requires_login(self, client):
        resp = client.get(HOME)
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_lists_delivered_session(self, client):
        s = seed(session_name="Lower")
        client.force_login(s.athlete)
        resp = client.get(HOME)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Hypertrophy Block" in body
        assert "Lower" in body
        # The session links through to its detail page.
        assert session_url(s.session) in body

    def test_shows_coach_name(self, client):
        s = seed()
        s.coach.name = "Coach Lance"
        s.coach.save(update_fields=["name"])
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "Coach Lance" in body

    def test_hides_undelivered_week(self, client):
        s = seed(delivered=False, session_name="SecretLower")
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        # The plan card may show (awaiting delivery) but its sessions must not.
        assert "SecretLower" not in body
        assert session_url(s.session) not in body

    def test_excludes_other_athletes_plan(self, client):
        mine = seed(session_name="MyLower")
        theirs = seed(session_name="TheirLower")
        client.force_login(mine.athlete)
        body = client.get(HOME).content.decode()
        assert "MyLower" in body
        assert "TheirLower" not in body
        assert session_url(theirs.session) not in body

    def test_excludes_inactive_link(self, client):
        s = seed(
            link_status=CoachAthlete.Status.PENDING_COACH_INVITE,
            session_name="PendingLower",
        )
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "PendingLower" not in body

    def test_excludes_archived_plan(self, client):
        s = seed(plan_status=Plan.Status.ARCHIVED, session_name="ArchivedLower")
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "ArchivedLower" not in body

    def test_session_status_reflects_log(self, client):
        s = seed()
        SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "Logged" in body

    def test_session_status_pending_without_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "To do" in body

    def test_another_athletes_log_does_not_mark_done(self, client):
        """A log belonging to a *different* athlete must not flip my session done."""
        s = seed()
        other = UserFactory()
        SessionLogFactory(
            session=s.session, athlete=other, status=SessionLog.Status.DONE
        )
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "To do" in body

    # -- multi-week block (P3) --------------------------------------------

    def test_shows_every_delivered_week_of_the_block(self, client):
        """The card renders the WHOLE delivered block, not just the latest week.

        The read-only multi-week table has a column per delivered week, each
        carrying that week's own prescription summary.
        """
        b = seed_block()
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        # A column per delivered week (labels + their distinct per-week text).
        assert "Wk 1" in body
        assert "Wk 2" in body
        assert "4 x 8, RPE 7, 71" in body  # week-1 cell summary (verbatim text)
        assert "4 x 8, RPE 8, 101" in body  # week-2 cell summary

    def test_undelivered_week_is_not_a_column(self, client):
        """A week the coach hasn't delivered yet never becomes an athlete column."""
        b = seed_block()  # week 3 undelivered
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Wk 3" not in body
        assert "RPE 8, 131" not in body  # its cell is never rendered

    def test_home_focuses_the_current_delivered_week(self, client):
        """The home opens to ``is_current``: only its sessions are tappable rows.

        Earlier delivered weeks live in the read-only table — their sessions are
        cells, not links to the logger.
        """
        b = seed_block()  # week 2 is_current + delivered
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert session_url(b.s2) in body  # current week's session logs
        assert session_url(b.s1) not in body  # earlier week is read-only

    def test_skipped_cell_renders_em_dash(self, client):
        """A one-week skip shows an em-dash in its cell, not a prescription."""
        b = seed_block(skip_first=True)
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "—" in body  # em-dash present
        assert "RPE 7, 71" not in body  # the skipped week shows no text
        assert "RPE 8, 101" in body  # the other delivered week still does

    def test_substitution_sub_line_shows_in_the_cell(self, client):
        """A substitution sub-line folds into that week's cell summary."""
        b = seed_block(sub_second="Front Squat")
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Front Squat" in body  # the substitution sub-line
        assert "Box Squat" in body  # the slot row still labels the row

    def test_multiple_current_weeks_focus_the_latest_delivered(self, client):
        """The home opens to the newest delivered week when several are current.

        A group-materialized plan flags EVERY delivered week ``is_current``, so
        the home must not focus the earliest of them (regression: focusing
        ``current_week`` stranded the athlete on week 1 after week 2 was
        delivered).
        """
        b = seed_block()  # w1 & w2 delivered at the same ``now``; w2 is_current
        # Reproduce the group-sync anomaly: an earlier week is *also* current and
        # was delivered before w2.
        b.w1.is_current = True
        b.w1.delivered_at = timezone.now() - timedelta(days=1)
        b.w1.save(update_fields=["is_current", "delivered_at"])
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert session_url(b.s2) in body  # newest delivered week is the focus
        assert session_url(b.s1) not in body  # the earlier current week is not

    def test_current_pointer_in_an_earlier_block_anchors_that_block(self, client):
        """A "Make current" back to an earlier delivered block wins over latest.

        ``latest_delivered_week`` points at the newest block, but when the coach
        moves the (single) ``is_current`` pointer to a week in an earlier
        delivered block, the individual athlete's home must open to THAT block and
        week — not the most recent delivery. The newer block is still reachable
        (issue #456 Finding 1: the chip strip spans the whole plan), just not the
        anchor — its session is not a tappable log row until the athlete taps
        into it.
        """
        coach = UserFactory()
        athlete = UserFactory()
        rel = CoachAthleteFactory(
            coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        plan = PlanFactory(
            relationship=rel, title="Two-Block Plan", status=Plan.Status.ACTIVE
        )
        earlier = timezone.now() - timedelta(days=7)
        now = timezone.now()
        # Block A — delivered earlier, carries the athlete's current week.
        meso_a = MesocycleFactory(plan=plan, name="Base", order=0)
        slot_a = SessionSlot.objects.create(
            mesocycle=meso_a, day_number=1, name="Squat Day", bias="Quad", order=0
        )
        ex_a = ExerciseSlot.objects.create(
            session_slot=slot_a, name="Back Squat", order=0
        )
        w_a = WeekFactory(
            mesocycle=meso_a, index=1, is_current=True, delivered_at=earlier
        )
        s_a = day(w_a, session_slot=slot_a)
        make_presc(
            exercise_slot=ex_a, week=w_a, sets="5", reps="5", load="100", rpe="8"
        )
        # Block B — delivered later, no current week.
        meso_b = MesocycleFactory(plan=plan, name="Peak", order=1)
        slot_b = SessionSlot.objects.create(
            mesocycle=meso_b, day_number=1, name="Bench Day", bias="Push", order=0
        )
        ex_b = ExerciseSlot.objects.create(
            session_slot=slot_b, name="Bench Press", order=0
        )
        w_b = WeekFactory(mesocycle=meso_b, index=1, is_current=False, delivered_at=now)
        s_b = day(w_b, session_slot=slot_b)
        make_presc(exercise_slot=ex_b, week=w_b, sets="3", reps="5", load="80", rpe="8")

        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert "Base" in body  # anchored on block A (the current pointer)
        assert "Back Squat" in body
        assert session_url(s_a) in body  # its week is the tappable log row
        assert "Peak" in body  # the newer block is reachable via the chip strip
        assert session_url(s_b) not in body  # but not a tappable row (not the anchor)

    def test_future_only_add_this_week_row_is_not_leaked(self, client):
        """An exercise added only to an undelivered future week must not surface.

        "Add this week only" seeds skipped placeholder cells in every other live
        week; when the target is an undelivered future week, the athlete's block
        table (delivered columns only) would otherwise render that build-ahead
        exercise's name across em-dash cells. A row with no trainable delivered
        cell is dropped.
        """
        b = seed_block()  # weeks 1 & 2 delivered, week 3 undelivered
        future = ExerciseSlot.objects.create(
            session_slot=b.slot, name="Front Squat (future only)", order=1
        )
        # Skipped placeholders in the delivered weeks; trainable only in the
        # undelivered week 3 (the add-this-week-only-a-future-week shape).
        make_presc(exercise_slot=future, week=b.w1, skipped=True)
        make_presc(exercise_slot=future, week=b.w2, skipped=True)
        make_presc(exercise_slot=future, week=b.w3, sets="3", reps="8", load="90")
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Front Squat (future only)" not in body  # build-ahead not leaked
        assert "Box Squat" in body  # the real delivered row still shows

    def test_block_table_comment_does_not_leak_onto_the_page(self, client):
        """The block table's template-author note stays out of the rendered HTML.

        Django's ``{# #}`` comment is single-line only, so a multi-line one would
        render verbatim onto the athlete's screen; the note must use
        ``{% comment %}`` (regression guard).
        """
        b = seed_block()
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Read-only multi-week" not in body
        assert "server-rendered" not in body
        assert "#}" not in body

    # -- nit #456 P2: an empty focused week must not hide navigation --------

    def test_focused_week_with_no_live_sessions_still_shows_chips_and_grid(
        self, client
    ):
        """A focused week with zero live sessions keeps plan-wide navigation.

        When the focused/current week's sessions are all soft-deleted but the
        plan still has another delivered week, the card must keep the chip
        strip and the block grid — hiding them (the old all-or-nothing
        ``{% if plan.sessions %}`` gate) would strand the athlete on the empty
        week with no way to reach the rest of the block.
        """
        b = seed_block()  # w1 & w2 delivered, w2 is_current
        b.s2.deleted_at = timezone.now()
        b.s2.save(update_fields=["deleted_at"])
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "No sessions this week." in body
        assert "Nothing delivered yet" not in body
        # The chip strip (2 delivered weeks) survives — week 1 is reachable.
        assert f"?week={b.w1.pk}" in body
        # The block grid survives too.
        assert "Your block" in body

    def test_nothing_delivered_still_shows_awaiting_copy(self, client):
        """The truly-nothing-delivered state keeps its own distinct copy."""
        s = seed(delivered=False)
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "Nothing delivered yet — your coach is still building this week." in body


# -- issue #456: ?week= display-only focus override + the chip/nudge UI ---


def log_url(session):
    return reverse("meso:athlete_log_session", kwargs={"pk": session.pk})


def post_log(client, session, payload):
    return client.post(
        log_url(session),
        data=json.dumps(payload),
        content_type="application/json",
    )


class TestWeekFocusOverride:
    """``?week=<id>`` opens a card onto a different delivered week — display only.

    Never moves ``is_current``: that pointer only ever advances via the
    athlete's own logging (auto-advance, #456) or the coach's "Make current".
    """

    def test_valid_later_week_switches_focus_without_moving_is_current(self, client):
        b = seed_block(third_delivered=True)  # w2 is_current; w1 & w3 delivered too
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": b.w3.pk})
        assert resp.status_code == 200
        body = resp.content.decode()
        assert session_url(b.s3) in body  # focus switched to week 3
        assert session_url(b.s2) not in body
        b.w2.refresh_from_db()
        b.w3.refresh_from_db()
        assert b.w2.is_current is True  # the GET never moved the pointer
        assert b.w3.is_current is False
        assert Week.objects.filter(mesocycle__plan=b.plan, is_current=True).count() == 1

    def test_valid_earlier_week_switches_focus_too(self, client):
        b = seed_block(third_delivered=True)  # w2 is_current; w1 is earlier
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": b.w1.pk})
        assert resp.status_code == 200
        body = resp.content.decode()
        assert session_url(b.s1) in body
        assert session_url(b.s2) not in body
        b.w2.refresh_from_db()
        assert b.w2.is_current is True  # review direction is still a no-op on write

    def test_nonexistent_week_id_is_ignored(self, client):
        b = seed_block()
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": 999999})
        assert resp.status_code == 200
        assert session_url(b.s2) in resp.content.decode()  # bare default focus

    def test_non_numeric_week_param_is_ignored(self, client):
        b = seed_block()
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": "not-an-id"})
        assert resp.status_code == 200
        assert session_url(b.s2) in resp.content.decode()

    def test_another_athletes_week_is_ignored(self, client):
        mine = seed_block()
        theirs = seed_block()
        client.force_login(mine.athlete)
        resp = client.get(HOME, {"week": theirs.w1.pk})
        assert resp.status_code == 200
        body = resp.content.decode()
        assert session_url(mine.s2) in body  # my own card renders as normal
        assert session_url(theirs.s1) not in body
        assert session_url(theirs.s2) not in body

    def test_undelivered_week_is_ignored(self, client):
        b = seed_block()  # week 3 left undelivered by default
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": b.w3.pk})
        assert resp.status_code == 200
        assert session_url(b.s2) in resp.content.decode()

    def test_soft_deleted_week_is_ignored(self, client):
        b = seed_block(third_delivered=True)
        b.w3.deleted_at = timezone.now()
        b.w3.save(update_fields=["deleted_at"])
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": b.w3.pk})
        assert resp.status_code == 200
        assert session_url(b.s2) in resp.content.decode()

    def test_override_is_scoped_to_its_own_plans_card(self, client):
        """Plan A's ``?week=`` override never disturbs plan B's card."""
        athlete = UserFactory()
        a = seed_block(athlete=athlete, third_delivered=True)
        b = seed_block(athlete=athlete, third_delivered=True)
        client.force_login(athlete)
        body = client.get(HOME, {"week": a.w3.pk}).content.decode()
        assert session_url(a.s3) in body  # plan A switched to week 3
        assert session_url(a.s2) not in body
        assert session_url(b.s2) in body  # plan B is untouched — still its own current
        assert session_url(b.s3) not in body


class TestWeekChips:
    """The tappable chip row above the session list — the universal navigation path."""

    def test_rendered_with_current_and_focused_marked(self, client):
        b = seed_block()  # w1, w2 delivered; w2 is_current + focus
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Wk 1" in body
        assert "Wk 2" in body
        assert f"?week={b.w1.pk}" in body  # the non-current chip carries the param
        assert f"?week={b.w2.pk}" not in body  # the current chip links to the bare URL

    def test_focused_week_distinguished_from_current(self, client):
        b = seed_block(third_delivered=True)  # w2 current; view w1 instead
        client.force_login(b.athlete)
        body = client.get(HOME, {"week": b.w1.pk}).content.decode()
        # The current week's chip still points at the bare URL...
        assert f"?week={b.w2.pk}" not in body
        # ...while the now-focused week's chip links back to itself.
        assert f"?week={b.w1.pk}" in body

    def test_not_rendered_for_a_single_delivered_week(self, client):
        s = seed()  # only one delivered week
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "?week=" not in body

    def test_undelivered_week_never_appears_as_a_chip(self, client):
        b = seed_block()  # week 3 left undelivered
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Wk 3" not in body
        assert f"?week={b.w3.pk}" not in body

    def test_soft_deleted_delivered_week_never_appears_as_a_chip(self, client):
        b = seed_block(third_delivered=True)
        b.w3.deleted_at = timezone.now()
        b.w3.save(update_fields=["deleted_at"])
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Wk 3" not in body
        assert f"?week={b.w3.pk}" not in body

    def test_chip_comment_does_not_leak_onto_the_page(self, client):
        b = seed_block()
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "tappable path onto any delivered" not in body
        assert "#}" not in body


class TestStartNextWeekNudge:
    """The "start next week" link appears only once the focus week is fully logged."""

    def test_absent_when_not_all_focus_sessions_done(self, client):
        b = seed_block(third_delivered=True)  # w2 focus, nothing logged
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "start Week" not in body

    def test_absent_when_focus_is_the_last_delivered_week(self, client):
        b = seed_block()  # w2 is focus + the LAST delivered week (w3 undelivered)
        SessionLogFactory(
            session=b.s2, athlete=b.athlete, status=SessionLog.Status.DONE
        )
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "start Week" not in body

    def test_appears_when_focus_done_and_a_later_week_exists(self, client):
        b = seed_block(third_delivered=True)  # w2 focus; w3 delivered + later
        SessionLogFactory(
            session=b.s2, athlete=b.athlete, status=SessionLog.Status.DONE
        )
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "start Week 3" in body
        assert f"?week={b.w3.pk}" in body


class TestWeekFocusIntegrationLoop:
    """Tap a later week's chip, log a session there, and auto-advance closes the loop."""

    def test_visiting_via_week_param_then_logging_advances_is_current(self, client):
        b = seed_block(third_delivered=True)  # w2 is_current; w3 later, delivered
        client.force_login(b.athlete)

        # The athlete taps the Wk 3 chip...
        body = client.get(HOME, {"week": b.w3.pk}).content.decode()
        assert session_url(b.s3) in body

        # ...and logs its session (already-shipped auto-advance, #456).
        resp = post_log(client, b.s3, {"sets": []})
        assert resp.status_code == 200

        b.w2.refresh_from_db()
        b.w3.refresh_from_db()
        assert b.w3.is_current is True
        assert b.w2.is_current is False
        assert Week.objects.filter(mesocycle__plan=b.plan, is_current=True).count() == 1

        # Their next bare visit naturally focuses week 3.
        body = client.get(HOME).content.decode()
        assert session_url(b.s3) in body
        assert session_url(b.s2) not in body


# -- issue #456 Finding 1: navigation spans the whole PLAN, not just the ----
# -- anchored block (a newly delivered block never moves is_current) -------


class TestPlanWideChips:
    """The chip strip spans every delivered week of the plan, grouped by block."""

    def test_chips_span_both_blocks_with_block_labels(self, client):
        b = seed_two_block()  # a2 (block A) is_current; block B has one more
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Base" in body
        assert "Peak" in body
        assert f"?week={b.a1.pk}" in body  # block A's earlier week
        assert f"?week={b.b1.pk}" in body  # block B's week is reachable too
        assert "Wk 1" in body
        assert "Wk 2" in body

    def test_override_into_a_different_block_renders_that_blocks_grid_and_sessions(
        self, client
    ):
        b = seed_two_block()
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": b.b1.pk})
        assert resp.status_code == 200
        body = resp.content.decode()
        assert session_url(b.sb1) in body  # block B's session is now tappable
        assert session_url(b.sa2) not in body
        assert "Bench Press" in body  # block B's grid
        assert "Back Squat" not in body  # block A's grid is not also shown
        # The GET never moved the pointer.
        b.a2.refresh_from_db()
        b.b1.refresh_from_db()
        assert b.a2.is_current is True
        assert b.b1.is_current is False
        assert Week.objects.filter(mesocycle__plan=b.plan, is_current=True).count() == 1


class TestPlanWideNudge:
    """The "start next week" nudge crosses block boundaries too."""

    def test_nudge_crosses_into_the_next_delivered_block(self, client):
        b = seed_two_block()  # a2 = last delivered week of block A, is_current
        SessionLogFactory(
            session=b.sa2, athlete=b.athlete, status=SessionLog.Status.DONE
        )
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "start Peak · Week 1" in body  # block-aware copy
        assert f"?week={b.b1.pk}" in body


class TestPlanWideIntegrationLoop:
    """Tapping into a later BLOCK, logging there, and auto-advance crossing it."""

    def test_full_http_loop_logs_into_block_two_and_advances_across_blocks(
        self, client
    ):
        b = seed_two_block()
        client.force_login(b.athlete)

        # The athlete taps into block B via the override...
        body = client.get(HOME, {"week": b.b1.pk}).content.decode()
        assert session_url(b.sb1) in body

        # ...and logs its session — auto-advance follows them across the block.
        resp = post_log(client, b.sb1, {"sets": []})
        assert resp.status_code == 200

        b.a2.refresh_from_db()
        b.b1.refresh_from_db()
        assert b.b1.is_current is True
        assert b.a2.is_current is False
        assert Week.objects.filter(mesocycle__plan=b.plan, is_current=True).count() == 1

        # Their next bare visit naturally focuses block B.
        body = client.get(HOME).content.decode()
        assert session_url(b.sb1) in body
        assert session_url(b.sa2) not in body
        assert "Peak" in body


class TestPlanWideGroupMember:
    """A group member's materialized plan gets the same plan-wide navigation."""

    def test_second_delivered_block_reachable_and_advances_the_members_own_pointer(
        self, client
    ):
        from store_project.meso.tests.test_group_deliver import seed_group
        from store_project.meso.tests.test_group_deliver import shared_meso

        group, plan, [m] = seed_group(member_count=1)
        meso1 = shared_meso(plan)
        group.deliver_block()  # first materialization mirrors is_current onto block 1
        athlete = m.relationship.athlete

        member_plan = Plan.objects.get(relationship=m.relationship, source_group=group)
        member_week1 = member_plan.mesocycles.get(order=meso1.order).weeks.get(
            is_current=True
        )

        # The coach ships a brand-new mesocycle (block 2) and delivers it.
        meso2 = MesocycleFactory(plan=plan, name="Peak", order=1)
        slot2 = SessionSlot.objects.create(
            mesocycle=meso2, day_number=1, name="Bench Day", bias="Push", order=0
        )
        ex2 = ExerciseSlot.objects.create(
            session_slot=slot2, name="Bench Press", order=0
        )
        src_week2 = WeekFactory(
            mesocycle=meso2, index=1, is_current=False, delivered_at=timezone.now()
        )
        day(src_week2, session_slot=slot2)
        make_presc(
            exercise_slot=ex2, week=src_week2, sets="3", reps="5", load="80", rpe="8"
        )

        # Second materialization: is_current is NOT re-mirrored (post-first-sync rule).
        _, member_weeks2 = m.sync_delivered_plan(meso2)
        member_week2 = member_weeks2[0]
        member_week2.delivered_at = timezone.now()
        member_week2.save(update_fields=["delivered_at"])

        member_week1.refresh_from_db()
        member_week2.refresh_from_db()
        assert member_week1.is_current is True
        assert member_week2.is_current is False

        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert "Peak" in body  # block 2's chip group is reachable
        assert f"?week={member_week2.pk}" in body

        resp = client.get(HOME, {"week": member_week2.pk})
        assert resp.status_code == 200
        assert "Bench Press" in resp.content.decode()

        session2 = member_week2.sessions.first()
        resp = post_log(client, session2, {"sets": []})
        assert resp.status_code == 200

        member_week1.refresh_from_db()
        member_week2.refresh_from_db()
        assert member_week2.is_current is True
        assert member_week1.is_current is False
        assert (
            Week.objects.filter(
                mesocycle__plan=member_plan, is_current=True, deleted_at__isnull=True
            ).count()
            == 1
        )

        # The shared plan's own pointer is untouched by the member's log.
        shared_week1 = meso1.weeks.get(index=1)
        assert shared_week1.is_current is True


# -- athlete session detail ------------------------------------------------


class TestAthleteSession:
    def test_requires_login(self, client):
        s = seed()
        resp = client.get(session_url(s.session))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_shows_prescribed_exercises(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = client.get(session_url(s.session))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Box Squat" in body
        assert "Lower" in body
        # The prescribed target is the cell's freeform text, verbatim.
        assert "4 x 6, RPE 7, 70" in body

    def test_404_for_other_athlete(self, client):
        s = seed()
        intruder = seed().athlete
        client.force_login(intruder)
        assert client.get(session_url(s.session)).status_code == 404

    def test_404_when_undelivered(self, client):
        s = seed(delivered=False)
        client.force_login(s.athlete)
        assert client.get(session_url(s.session)).status_code == 404

    def test_404_inactive_link(self, client):
        s = seed(link_status=CoachAthlete.Status.ENDED)
        client.force_login(s.athlete)
        assert client.get(session_url(s.session)).status_code == 404

    def test_404_archived_plan(self, client):
        s = seed(plan_status=Plan.Status.ARCHIVED)
        client.force_login(s.athlete)
        assert client.get(session_url(s.session)).status_code == 404

    def test_404_unknown_session(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert (
            client.get(
                reverse("meso:athlete_session", kwargs={"pk": 999999})
            ).status_code
            == 404
        )


# -- roster routing for pure athletes --------------------------------------


class TestRosterRedirect:
    def test_pure_athlete_redirected_to_home(self, client):
        s = seed()
        # ``s.athlete`` has an active coach link but no CoachProfile.
        client.force_login(s.athlete)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 302
        assert resp.url == HOME

    def test_coach_stays_on_roster(self, client):
        s = seed()
        CoachProfileFactory(user=s.coach)
        client.force_login(s.coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200

    def test_coach_who_is_also_athlete_stays_on_roster(self, client):
        s = seed()
        # The coach also trains under someone else *and* coaches — stays on roster.
        CoachProfileFactory(user=s.coach)
        seed(athlete=s.coach)
        client.force_login(s.coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200


# -- soft delete (designer framework Phase 0) --------------------------------


class TestSoftDeletedRowsHidden:
    """A coach's soft-delete must reach the athlete surface, not just the designer.

    Deleting an already-delivered day/exercise stamps ``deleted_at``; the
    athlete must stop seeing it — home no longer lists the day, its logger
    404s, and log/1RM writes against a deleted row are rejected — while every
    log already recorded survives (that survival is pinned by the designer
    delete tests; here we pin the *hiding*).
    """

    def _log_post(self, client, session, payload):
        import json

        return client.post(
            reverse("meso:athlete_log_session", kwargs={"pk": session.pk}),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_home_hides_soft_deleted_session(self, client):
        # The tappable "log today" row/link for a soft-deleted Session must be
        # gone. This does NOT assert the day's name disappears from the page
        # entirely: since nit #456 P2, the read-only block-wide grid renders
        # independent of the focus week's live ``Session`` rows (it's driven by
        # the block-level ``SessionSlot``/``ExerciseSlot``/``Prescription``,
        # none of which this test touches), so the day can legitimately still
        # appear there even though its per-week log affordance is hidden.
        s = seed(session_name="Lower")
        s.session.deleted_at = timezone.now()
        s.session.save(update_fields=["deleted_at"])
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert session_url(s.session) not in body
        assert "No sessions this week." in body

    def test_session_page_404s_when_session_soft_deleted(self, client):
        s = seed()
        s.session.deleted_at = timezone.now()
        s.session.save(update_fields=["deleted_at"])
        client.force_login(s.athlete)
        assert client.get(session_url(s.session)).status_code == 404

    def test_session_page_404s_when_week_soft_deleted(self, client):
        s = seed()
        s.week.deleted_at = timezone.now()
        s.week.save(update_fields=["deleted_at"])
        client.force_login(s.athlete)
        assert client.get(session_url(s.session)).status_code == 404

    def test_logger_hides_soft_deleted_prescription(self, client):
        s = seed()
        ghost = make_presc(s.session, name="Ghost Curl")
        ghost.exercise_slot.soft_delete()
        client.force_login(s.athlete)
        body = client.get(session_url(s.session)).content.decode()
        assert "Ghost Curl" not in body
        assert "Box Squat" in body

    def test_logging_a_soft_deleted_prescription_is_rejected(self, client):
        s = seed()
        ghost = make_presc(s.session, name="Ghost Curl")
        ghost.exercise_slot.soft_delete()
        client.force_login(s.athlete)
        resp = self._log_post(
            client,
            s.session,
            {"sets": [{"prescription": ghost.pk, "reps": "8", "load": "60"}]},
        )
        assert resp.status_code == 400
        assert SessionLog.objects.count() == 0

    def test_one_rm_on_a_soft_deleted_prescription_is_rejected(self, client):
        s = seed()
        ghost = make_presc(s.session, name="Ghost Curl")
        ghost.exercise_slot.soft_delete()
        client.force_login(s.athlete)
        import json

        resp = client.post(
            reverse("meso:athlete_set_one_rm", kwargs={"pk": s.session.pk}),
            data=json.dumps({"prescription": ghost.pk, "value": "140"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_resave_preserves_a_hidden_prescriptions_logged_sets(self, client):
        s = seed()
        ghost = make_presc(s.session, name="Ghost Curl")
        client.force_login(s.athlete)
        resp = self._log_post(
            client,
            s.session,
            {
                "sets": [
                    {"prescription": s.presc.pk, "reps": "6", "load": "70"},
                    {"prescription": ghost.pk, "reps": "8", "load": "40"},
                ]
            },
        )
        assert resp.status_code == 200

        # The coach removes Ghost Curl; the logger no longer shows it, so the
        # athlete's next save posts only the surviving lift. The hidden row's
        # already-logged set must NOT be wiped by the save's replace step.
        ghost.exercise_slot.soft_delete()
        resp = self._log_post(
            client,
            s.session,
            {"sets": [{"prescription": s.presc.pk, "reps": "5", "load": "75"}]},
        )
        assert resp.status_code == 200

        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        ghost_sets = log.sets.filter(prescription=ghost)
        assert ghost_sets.count() == 1
        assert ghost_sets.get().reps == "8"
        live_sets = log.sets.filter(prescription=s.presc)
        assert live_sets.count() == 1
        assert live_sets.get().reps == "5"
