"""Coach-facing adherence — cadence (recency + rolling volume) + the activity feed.

These pin the read-side aggregation behind the roster's "last trained" pill and
the profile's cadence chip — replacing the old current-week compliance meter,
which lost its denominator once ``Week.is_current`` was removed (programs are
date-less; docs/meso/remove-current-week-plan.md §4a, decided 2026-07-18):

- ``adherence.link_last_trained`` — the athlete's most recent *done*
  ``SessionLog``, across every live (non-archived) plan on the link;
- ``adherence.link_recency_days`` — whole days since that log, the roster
  pill's tone-band input;
- ``adherence.link_session_count`` — distinct DONE sessions in a rolling
  window (default 14d), the profile's secondary chip;
- ``adherence.recent_logs`` / ``presenters.roster_activity`` — the coach's
  athletes' most recent completed sessions, scoped to active links
  (untouched by this rework — already fully date-less).

The contract mirrors the athlete surface: ``done`` is the signal, and an
athlete only ever counts their own logs.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import adherence
from store_project.meso import presenters
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.users.factories import UserFactory

from ._helpers import day

pytestmark = pytest.mark.django_db


def delivered_week(
    rel, *, sessions=3, done=0, delivered_at=None, index=1, archived=False
):
    """A delivered week under ``rel`` with ``sessions`` days, ``done`` of them logged.

    Returns the ``Week``. The first ``done`` sessions get a *done* ``SessionLog``
    for the relationship's athlete; the rest are left un-logged. ``archived``
    parks the plan in the ARCHIVED state (e.g. a removed group member's
    materialized snapshot).
    """
    plan_status = Plan.Status.ARCHIVED if archived else Plan.Status.DRAFT
    meso = MesocycleFactory(
        plan=PlanFactory(relationship=rel, status=plan_status), order=index
    )
    week = WeekFactory(
        mesocycle=meso,
        index=index,
        delivered_at=delivered_at or timezone.now(),
    )
    for n in range(sessions):
        session = day(week, day_number=n + 1, name=f"Day {n + 1}")
        if n < done:
            SessionLogFactory(
                session=session,
                athlete=rel.athlete,
                status=SessionLog.Status.DONE,
            )
    return week


def _backdate(log, days_ago):
    """Force a ``SessionLog.created_at`` back in time.

    Bypasses ``auto_now_add``, which otherwise ignores any value passed at
    creation.
    """
    SessionLog.objects.filter(pk=log.pk).update(
        created_at=timezone.now() - timedelta(days=days_ago)
    )
    log.refresh_from_db()
    return log


def _done_log(rel, *, days_ago=0, week=None):
    """A single DONE ``SessionLog`` for ``rel``'s athlete, optionally back-dated."""
    week = week or delivered_week(rel, sessions=1, done=0)
    session = week.sessions.first()
    log = SessionLogFactory(
        session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
    )
    if days_ago:
        _backdate(log, days_ago)
    return log


# -- link_last_trained / link_recency_days -----------------------------------


class TestLinkLastTrainedAndRecency:
    def test_none_without_any_logs(self):
        rel = CoachAthleteFactory()
        assert adherence.link_last_trained(rel) is None
        assert adherence.link_recency_days(rel) is None

    def test_none_link_is_none(self):
        assert adherence.link_last_trained(None) is None
        assert adherence.link_recency_days(None) is None

    def test_recency_zero_for_a_log_today(self):
        rel = CoachAthleteFactory()
        _done_log(rel)
        assert adherence.link_recency_days(rel) == 0

    def test_recency_counts_whole_days(self):
        rel = CoachAthleteFactory()
        _done_log(rel, days_ago=5)
        assert adherence.link_recency_days(rel) == 5

    def test_picks_the_newest_log_across_plans(self):
        rel = CoachAthleteFactory()
        _done_log(rel, days_ago=10)
        newest = _done_log(rel, days_ago=1)
        assert adherence.link_last_trained(rel).pk == newest.pk
        assert adherence.link_recency_days(rel) == 1

    def test_ignores_pending_logs(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=1, done=0)
        session = week.sessions.first()
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.PENDING
        )
        assert adherence.link_last_trained(rel) is None

    def test_ignores_another_athletes_logs(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=1, done=0)
        session = week.sessions.first()
        stranger = UserFactory()
        SessionLogFactory(
            session=session, athlete=stranger, status=SessionLog.Status.DONE
        )
        assert adherence.link_last_trained(rel) is None

    def test_ignores_archived_plans(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=1, done=0, archived=True)
        session = week.sessions.first()
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )
        assert adherence.link_last_trained(rel) is None

    def test_spans_every_live_plan_not_just_the_newest(self):
        # An older still-live plan's log can be the freshest one — cadence
        # isn't scoped to "the newest plan" the way the old meter was.
        rel = CoachAthleteFactory()
        older_week = delivered_week(rel, sessions=1, done=0, index=1)
        newer_week = delivered_week(rel, sessions=1, done=0, index=1)
        newest_log = _done_log(rel, week=older_week, days_ago=1)
        _done_log(rel, week=newer_week, days_ago=20)
        assert adherence.link_last_trained(rel).pk == newest_log.pk


# -- link_session_count -------------------------------------------------------


class TestLinkSessionCount:
    def test_zero_without_logs(self):
        rel = CoachAthleteFactory()
        assert adherence.link_session_count(rel) == 0

    def test_zero_link_is_none(self):
        assert adherence.link_session_count(None) == 0

    def test_counts_done_logs_within_the_window(self):
        rel = CoachAthleteFactory()
        _done_log(rel, days_ago=1)
        _done_log(rel, days_ago=13)
        assert adherence.link_session_count(rel, days=14) == 2

    def test_excludes_logs_outside_the_window(self):
        rel = CoachAthleteFactory()
        _done_log(rel, days_ago=15)
        assert adherence.link_session_count(rel, days=14) == 0

    def test_ignores_pending_logs(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=1, done=0)
        session = week.sessions.first()
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.PENDING
        )
        assert adherence.link_session_count(rel) == 0

    def test_ignores_archived_plans(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=1, done=0, archived=True)
        session = week.sessions.first()
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )
        assert adherence.link_session_count(rel) == 0

    def test_duplicate_done_logs_count_the_session_once(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=1, done=0)
        session = week.sessions.first()
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )
        assert adherence.link_session_count(rel) == 1


# -- recent_logs (unchanged — already fully date-less) ------------------------


class TestRecentLogs:
    def test_only_done_logs(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=2, done=1)
        # The second session has only a pending log.
        pending_session = week.sessions.order_by("session_slot__day_number").last()
        SessionLogFactory(
            session=pending_session,
            athlete=rel.athlete,
            status=SessionLog.Status.PENDING,
        )
        logs = adherence.recent_logs(rel.coach)
        assert len(logs) == 1
        assert all(log.status == SessionLog.Status.DONE for log in logs)

    def test_scoped_to_this_coach(self):
        mine = CoachAthleteFactory()
        delivered_week(mine, sessions=1, done=1)
        other = CoachAthleteFactory()
        delivered_week(other, sessions=1, done=1)
        logs = adherence.recent_logs(mine.coach)
        assert len(logs) == 1
        assert logs[0].athlete_id == mine.athlete_id

    def test_excludes_ended_relationships(self):
        coach = UserFactory()
        ended = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ENDED)
        delivered_week(ended, sessions=1, done=1)
        assert adherence.recent_logs(coach) == []

    def test_newest_first_and_limited(self):
        rel = CoachAthleteFactory()
        # 10 done sessions in one delivered week → 10 done logs, created in order.
        delivered_week(rel, sessions=10, done=10)
        logs = adherence.recent_logs(rel.coach, limit=3)
        assert len(logs) == 3
        created = [log.created_at for log in logs]
        assert created == sorted(created, reverse=True)

    def test_excludes_archived_plans(self):
        rel = CoachAthleteFactory()
        delivered_week(rel, sessions=1, done=1, archived=True)
        # A removed group member's archived snapshot must not resurface as
        # "recent activity."
        assert adherence.recent_logs(rel.coach) == []

    def test_excludes_logs_by_a_non_linked_athlete(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=1, done=0)
        session = week.sessions.first()
        stranger = UserFactory()
        # A done log on the coach's session but by someone other than the plan's
        # athlete (the model permits it; the write path never does) must not leak
        # into the feed — it would show an unrelated name + an unreachable link.
        SessionLogFactory(
            session=session, athlete=stranger, status=SessionLog.Status.DONE
        )
        assert adherence.recent_logs(rel.coach) == []

    def test_spans_multiple_athletes(self):
        coach = UserFactory()
        a = CoachAthleteFactory(coach=coach)
        b = CoachAthleteFactory(coach=coach)
        delivered_week(a, sessions=1, done=1)
        delivered_week(b, sessions=1, done=1)
        logs = adherence.recent_logs(coach)
        assert {log.athlete_id for log in logs} == {a.athlete_id, b.athlete_id}


# -- presenters ----------------------------------------------------------------


class TestRosterPresenters:
    def test_roster_athlete_threads_recency(self):
        user = UserFactory()
        row = presenters.roster_athlete(user, recency_days=2)
        assert row["recency_label"] == "Last trained 2 days ago"
        assert row["recency_tone"] == "ok"

    def test_roster_athlete_recency_defaults_to_no_sessions(self):
        user = UserFactory()
        row = presenters.roster_athlete(user)
        assert row["recency_label"] == "No sessions yet"
        assert row["recency_tone"] == "muted"

    def test_roster_athlete_recency_tone_bands(self):
        # <=3d green (ok), 4-9d amber (warn), >=10d red (danger) — decided
        # 2026-07-18 (docs/meso/remove-current-week-plan.md §4a).
        user = UserFactory()
        assert presenters.roster_athlete(user, recency_days=0)["recency_tone"] == "ok"
        assert presenters.roster_athlete(user, recency_days=3)["recency_tone"] == "ok"
        assert presenters.roster_athlete(user, recency_days=4)["recency_tone"] == "warn"
        assert presenters.roster_athlete(user, recency_days=9)["recency_tone"] == "warn"
        assert (
            presenters.roster_athlete(user, recency_days=10)["recency_tone"] == "danger"
        )

    def test_roster_athlete_recency_label_today_and_singular_day(self):
        user = UserFactory()
        assert (
            presenters.roster_athlete(user, recency_days=0)["recency_label"]
            == "Last trained today"
        )
        assert (
            presenters.roster_athlete(user, recency_days=1)["recency_label"]
            == "Last trained 1 day ago"
        )

    def test_roster_athlete_omits_contraindications(self):
        # Issue #382: contraindications belong on the athlete profile, not the
        # scannable roster row — the presenter must not carry them as flags.
        user = UserFactory()
        ContraindicationFactory(
            athlete=user, text="Cervical spine — avoid overhead pressing"
        )
        row = presenters.roster_athlete(user)
        assert "flags" not in row

    def test_roster_activity_shape(self):
        rel = CoachAthleteFactory()
        rel.athlete.name = "Maya Okonkwo"
        rel.athlete.save(update_fields=["name"])
        week = delivered_week(rel, sessions=1, done=0)
        session = week.sessions.first()
        session.session_slot.name = "Lower"
        session.session_slot.save(update_fields=["name"])
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )
        events = presenters.roster_activity(rel.coach)
        assert len(events) == 1
        ev = events[0]
        assert ev["athlete"]["name"] == "Maya Okonkwo"
        assert ev["athlete"]["initials"] == "MO"
        assert ev["athlete"]["id"] == rel.athlete.pk
        assert "Lower" in ev["text"]
        assert ev["when"]  # a non-empty relative-time label

    def test_roster_activity_falls_back_to_day_label(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=1, done=0)
        session = week.sessions.first()
        session.session_slot.name = ""  # a blank session name
        session.session_slot.save(update_fields=["name"])
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )
        events = presenters.roster_activity(rel.coach)
        assert events[0]["text"]  # never blank, even without a session name


# -- RosterView integration ------------------------------------------------


class TestRosterViewAdherence:
    def test_recency_pill_and_activity_render(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        rel = CoachAthleteFactory(coach=coach)
        rel.athlete.name = "Maya Okonkwo"
        rel.athlete.save(update_fields=["name"])
        week = delivered_week(rel, sessions=2, done=0)
        session = week.sessions.order_by("session_slot__day_number").first()
        session.session_slot.name = "Lower"
        session.session_slot.save(update_fields=["name"])
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )

        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        body = resp.content.decode()
        # The tone-coded "last trained" pill for the just-logged session.
        assert "Last trained today" in body
        # Recent-activity feed names the athlete + the session they logged.
        assert "Lower" in body

    def test_no_sessions_yet_renders_for_an_unlogged_athlete(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        CoachAthleteFactory(coach=coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        assert "No sessions yet" in resp.content.decode()
