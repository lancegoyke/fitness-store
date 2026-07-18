"""Athlete slice Phase 1 — the athlete's read surface.

The first logged-in surface for an *athlete* (distinct from the coach's view of
an athlete at ``/meso/athlete/<uuid>/``):

- ``/meso/me/`` lists the athlete's active-coach plans, each card opened onto a
  derived scroll hint (the last live week containing any of the athlete's OWN
  logged sessions, else the earliest live week — see ``presenters._scroll_hint``,
  docs/meso/remove-current-week-plan.md §5), with that week's sessions marked
  done/pending from the athlete's own ``SessionLog``. The app never asserts a
  "you are here" position — no "current"/"Week N"/"This week" label anywhere;
- ``/meso/me/session/<id>/`` shows one session's prescribed exercises.

These tests pin the **scoping contract**: an athlete sees only plans across
their *active* coaches, and never another athlete's data. Edits are live (2d,
parity plan §3.3): every live week is visible the moment the coach types it —
delivery is a one-time notify + snapshot, never a visibility gate.
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
    logged=True,
):
    """A three-week block of ONE day × ONE exercise row (P3 athlete multi-week).

    All three weeks share one ``SessionSlot`` (the day) and one ``ExerciseSlot``
    (the row), with a per-week ``Prescription`` cell carrying a distinct load so
    each week's column is identifiable. Weeks 1 & 2 are delivered (the whole
    block delivers at once); week 3 is left undelivered unless
    ``third_delivered`` — since 2d that only means "not yet nudged about"; the
    athlete sees it live either way.

    ``logged`` (default ``True``) stamps a DONE ``SessionLog`` on week 2's
    session, making it the derived scroll hint (§5) for the bulk of these
    tests — the real-data equivalent of the old ``is_current`` flag, but
    re-derived from the athlete's own logging rather than a stored pointer.
    Pass ``logged=False`` to test the true zero-history default (the
    earliest live week). ``sub_second`` adds a freeform sub-line (e.g. a
    substitution) beneath week 2's cell.
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
    w1 = WeekFactory(mesocycle=meso, index=1, delivered_at=now)
    w2 = WeekFactory(mesocycle=meso, index=2, delivered_at=now)
    w3 = WeekFactory(
        mesocycle=meso,
        index=3,
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
    if logged:
        SessionLogFactory(session=s2, athlete=athlete, status=SessionLog.Status.DONE)
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

    Block A ("Base", order 0) carries two delivered weeks. Block B ("Peak",
    order 1) carries one delivered week, index 1, delivered LATER than block
    A. Neither block has any logged session — the plan's derived scroll hint
    (§5) is therefore its earliest live week (block A, week 1) unless a test
    logs into one explicitly or overrides with ``?week=``. A newly delivered
    block is always reachable via plan-wide chip navigation regardless of the
    anchor (issue #456 Finding 1).
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
    a1 = WeekFactory(mesocycle=meso_a, index=1, delivered_at=now)
    a2 = WeekFactory(mesocycle=meso_a, index=2, delivered_at=now)
    sa1 = day(a1, session_slot=slot_a)
    sa2 = day(a2, session_slot=slot_a)
    make_presc(exercise_slot=ex_a, week=a1, sets="5", reps="5", load="90", rpe="8")
    make_presc(exercise_slot=ex_a, week=a2, sets="5", reps="5", load="100", rpe="8")

    meso_b = MesocycleFactory(plan=plan, name="Peak", order=1)
    slot_b = SessionSlot.objects.create(
        mesocycle=meso_b, day_number=1, name="Bench Day", bias="Push", order=0
    )
    ex_b = ExerciseSlot.objects.create(session_slot=slot_b, name="Bench Press", order=0)
    b1 = WeekFactory(mesocycle=meso_b, index=1, delivered_at=now)
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

    def test_undelivered_week_is_visible(self, client):
        """Edits are live (2d): a week the coach hasn't delivered still shows."""
        s = seed(delivered=False, session_name="FreshLower")
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "FreshLower" in body
        assert session_url(s.session) in body

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

    # -- no position claim anywhere (docs/meso/remove-current-week-plan.md) --

    def test_no_position_label_rendered(self, client):
        """The app never tells the athlete which week/position they're on."""
        s = seed()
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "This week" not in body
        assert "Week 2" not in body  # s.week is index 2 — no "Week N" claim
        assert "Your programs" in body  # the neutral heading instead

    # -- multi-week block (P3) --------------------------------------------

    def test_shows_every_delivered_week_of_the_block(self, client):
        """The card renders the WHOLE delivered block, not just the anchor week.

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

    def test_undelivered_week_is_a_column_too(self, client):
        """Edits are live (2d): an undelivered week is a column like any other."""
        b = seed_block()  # week 3 undelivered
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Wk 3" in body
        assert "4 x 8, RPE 8, 131" in body  # its cell renders live

    def test_home_focuses_the_last_logged_week(self, client):
        """The home opens on the derived scroll hint.

        Only its sessions are tappable rows — earlier weeks live in the
        read-only table, their sessions are cells, not links to the logger.
        """
        b = seed_block()  # week 2 has the default logged session (see seed_block)
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert session_url(b.s2) in body  # the logged week's session logs
        assert session_url(b.s1) not in body  # earlier week is read-only

    def test_home_focuses_the_earliest_week_when_nothing_logged(self, client):
        """With nothing logged, the scroll hint is the plan's earliest week.

        Never a "current" claim — just where the grid opens.
        """
        b = seed_block(logged=False)
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert session_url(b.s1) in body
        assert session_url(b.s2) not in body

    def test_anchors_on_the_latest_logged_week_when_multiple_are_logged(self, client):
        """The LATEST (plan order) logged week wins the scroll hint.

        Not the earliest, when the athlete has logged into more than one week.
        """
        b = seed_block()  # week 2 is logged by default
        SessionLogFactory(
            session=b.s1, athlete=b.athlete, status=SessionLog.Status.DONE
        )
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert session_url(b.s2) in body  # the later logged week wins
        assert session_url(b.s1) not in body

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

    def test_scroll_hint_follows_the_log_not_delivery_recency(self, client):
        """A log in an earlier block anchors there even a newer block delivered later.

        The scroll hint (§5) is derived purely from the athlete's own logged
        sessions — never from which block the coach delivered most recently —
        so a block the athlete actually trained in wins the anchor even when a
        brand-new block was delivered afterward. The newer block is still
        reachable via the chip strip; its session just isn't the tappable log
        row until the athlete taps into it.
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
        # Block A — delivered earlier; the athlete actually logged here.
        meso_a = MesocycleFactory(plan=plan, name="Base", order=0)
        slot_a = SessionSlot.objects.create(
            mesocycle=meso_a, day_number=1, name="Squat Day", bias="Quad", order=0
        )
        ex_a = ExerciseSlot.objects.create(
            session_slot=slot_a, name="Back Squat", order=0
        )
        w_a = WeekFactory(mesocycle=meso_a, index=1, delivered_at=earlier)
        s_a = day(w_a, session_slot=slot_a)
        make_presc(
            exercise_slot=ex_a, week=w_a, sets="5", reps="5", load="100", rpe="8"
        )
        SessionLogFactory(session=s_a, athlete=athlete, status=SessionLog.Status.DONE)
        # Block B — delivered later; nothing logged here yet.
        meso_b = MesocycleFactory(plan=plan, name="Peak", order=1)
        slot_b = SessionSlot.objects.create(
            mesocycle=meso_b, day_number=1, name="Bench Day", bias="Push", order=0
        )
        ex_b = ExerciseSlot.objects.create(
            session_slot=slot_b, name="Bench Press", order=0
        )
        w_b = WeekFactory(mesocycle=meso_b, index=1, delivered_at=now)
        s_b = day(w_b, session_slot=slot_b)
        make_presc(exercise_slot=ex_b, week=w_b, sets="3", reps="5", load="80", rpe="8")

        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert "Base" in body  # anchored on block A — where the athlete logged
        assert "Back Squat" in body
        assert session_url(s_a) in body  # its week is the tappable log row
        assert "Peak" in body  # the newer block is reachable via the chip strip
        assert session_url(s_b) not in body  # but not a tappable row (not the anchor)

    def test_row_skipped_in_every_week_is_hidden(self, client):
        """A row with no trainable cell anywhere never renders.

        A row skipped in every week (nothing but placeholder cells) would show
        a name beside a strip of em-dashes, so it's dropped. A row trainable in
        ANY live week — e.g. an "add this week only" exercise targeting a
        future week — renders (2d: every live week is a visible column, so
        there's no build-ahead leak to guard against).
        """
        b = seed_block()
        ghost = ExerciseSlot.objects.create(
            session_slot=b.slot, name="Ghost Row (all skipped)", order=1
        )
        make_presc(exercise_slot=ghost, week=b.w1, skipped=True)
        make_presc(exercise_slot=ghost, week=b.w2, skipped=True)
        make_presc(exercise_slot=ghost, week=b.w3, skipped=True)
        future = ExerciseSlot.objects.create(
            session_slot=b.slot, name="Front Squat (week 3 only)", order=2
        )
        make_presc(exercise_slot=future, week=b.w1, skipped=True)
        make_presc(exercise_slot=future, week=b.w2, skipped=True)
        make_presc(exercise_slot=future, week=b.w3, sets="3", reps="8", load="90")
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Ghost Row (all skipped)" not in body  # nothing trainable — dropped
        assert "Front Squat (week 3 only)" in body  # live in week 3's column
        assert "Box Squat" in body

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

        When the anchored week's sessions are all soft-deleted but the plan
        still has another delivered week, the card must keep the chip strip
        and the block grid — hiding them (the old all-or-nothing
        ``{% if plan.sessions %}`` gate) would strand the athlete on the empty
        week with no way to reach the rest of the block.
        """
        b = seed_block()  # w1 & w2 delivered; w2 is the (logged) anchor
        b.s2.deleted_at = timezone.now()
        b.s2.save(update_fields=["deleted_at"])
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "No sessions here yet." in body
        assert "Nothing delivered yet" not in body
        # The chip strip (2 delivered weeks) survives — week 1 is reachable.
        assert f"?week={b.w1.pk}" in body
        # The block grid survives too.
        assert "Your block" in body

    def test_plan_with_no_weeks_shows_awaiting_copy(self, client):
        """A plan with no live weeks at all keeps its own distinct copy."""
        coach = UserFactory()
        athlete = UserFactory()
        rel = CoachAthleteFactory(
            coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        plan = PlanFactory(
            relationship=rel, title="Bare Plan", status=Plan.Status.ACTIVE
        )
        MesocycleFactory(plan=plan, name="Empty Block", order=0)
        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert "Bare Plan" in body
        assert "Nothing here yet — your coach is still building this program." in body


# -- ?week= display-only override + the chip UI ----------------------------


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

    It never writes anything — there's no pointer left to move (docs/meso/
    remove-current-week-plan.md): the scroll hint is re-derived on every read
    from the athlete's own logged sessions, and the override simply wins over
    it for that one request.
    """

    def test_valid_later_week_switches_focus(self, client):
        b = seed_block(third_delivered=True)  # w2 is the default anchor
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": b.w3.pk})
        assert resp.status_code == 200
        body = resp.content.decode()
        assert session_url(b.s3) in body  # focus switched to week 3
        assert session_url(b.s2) not in body

    def test_valid_earlier_week_switches_focus_too(self, client):
        b = seed_block(third_delivered=True)  # w2 is the default anchor
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": b.w1.pk})
        assert resp.status_code == 200
        body = resp.content.decode()
        assert session_url(b.s1) in body
        assert session_url(b.s2) not in body

    def test_nonexistent_week_id_is_ignored(self, client):
        b = seed_block()
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": 999999})
        assert resp.status_code == 200
        assert session_url(b.s2) in resp.content.decode()  # bare default anchor

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

    def test_undelivered_week_is_focusable(self, client):
        """Edits are live (2d): any live week of the plan can take the focus."""
        b = seed_block()  # week 3 left undelivered by default
        client.force_login(b.athlete)
        resp = client.get(HOME, {"week": b.w3.pk})
        assert resp.status_code == 200
        body = resp.content.decode()
        assert session_url(b.s3) in body
        assert session_url(b.s2) not in body

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
        assert session_url(b.s2) in body  # plan B is untouched — still its own anchor
        assert session_url(b.s3) not in body


class TestWeekChips:
    """The tappable chip row above the session list — the universal navigation path."""

    def test_every_chip_links_with_the_week_param(self, client):
        b = seed_block()  # w1, w2 delivered; w2 is the (logged) anchor
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Wk 1" in body
        assert "Wk 2" in body
        # Every chip — including the anchored one — is a plain ?week= link; the
        # app has no "current" week to give a special bare-URL chip to
        # (docs/meso/remove-current-week-plan.md).
        assert f"?week={b.w1.pk}" in body
        assert f"?week={b.w2.pk}" in body

    def test_not_rendered_for_a_single_delivered_week(self, client):
        s = seed()  # only one delivered week
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "?week=" not in body

    def test_undelivered_week_appears_as_a_chip_too(self, client):
        """Edits are live (2d): every live week is a chip, delivered or not."""
        b = seed_block()  # week 3 left undelivered
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Wk 3" in body
        assert f"?week={b.w3.pk}" in body

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
        assert "tappable path onto any live" not in body
        assert "#}" not in body


# -- issue #456 Finding 1: navigation spans the whole PLAN, not just the ----
# -- anchored block ----------------------------------------------------------


class TestPlanWideChips:
    """The chip strip spans every delivered week of the plan, grouped by block."""

    def test_chips_span_both_blocks_with_block_labels(self, client):
        b = seed_two_block()
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

    def test_undelivered_session_is_reachable(self, client):
        """Edits are live (2d): delivery never gates the session view."""
        s = seed(delivered=False)
        client.force_login(s.athlete)
        resp = client.get(session_url(s.session))
        assert resp.status_code == 200
        assert "Box Squat" in resp.content.decode()

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
        assert "No sessions here yet." in body

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
