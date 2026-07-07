"""Coach-facing adherence — the roster ``compliance`` meter + ``activity`` feed.

These pin the read-side aggregation that finally fills the roster's two
long-standing placeholders (``presenters`` carried ``compliance=None`` and the
view set ``activity=[]`` as "Phase 2/3 concepts awaiting logged data"):

- ``adherence.link_compliance`` — what fraction of the latest *delivered* week's
  sessions the athlete has marked *done*;
- ``adherence.recent_logs`` / ``presenters.roster_activity`` — the coach's
  athletes' most recent completed sessions, scoped to active links.

The contract mirrors the athlete surface: delivery is the gate, ``done`` is the
signal, and an athlete only ever counts their own logs.
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


# -- link_compliance -------------------------------------------------------


class TestLinkCompliance:
    def test_none_without_a_plan(self):
        rel = CoachAthleteFactory()
        assert adherence.link_compliance(rel) is None

    def test_none_when_nothing_delivered(self):
        rel = CoachAthleteFactory()
        meso = MesocycleFactory(plan=PlanFactory(relationship=rel))
        WeekFactory(mesocycle=meso, delivered_at=None)  # built, never delivered
        assert adherence.link_compliance(rel) is None

    def test_none_for_a_delivered_week_with_no_sessions(self):
        rel = CoachAthleteFactory()
        meso = MesocycleFactory(plan=PlanFactory(relationship=rel))
        WeekFactory(mesocycle=meso, delivered_at=timezone.now())
        assert adherence.link_compliance(rel) is None

    def test_none_link_is_none(self):
        assert adherence.link_compliance(None) is None

    def test_zero_when_delivered_but_unlogged(self):
        rel = CoachAthleteFactory()
        delivered_week(rel, sessions=3, done=0)
        # A real, distinct signal — the coach delivered, the athlete hasn't logged.
        assert adherence.link_compliance(rel) == 0

    def test_full_when_all_done(self):
        rel = CoachAthleteFactory()
        delivered_week(rel, sessions=4, done=4)
        assert adherence.link_compliance(rel) == 100

    def test_rounds_partial(self):
        rel = CoachAthleteFactory()
        delivered_week(rel, sessions=3, done=2)  # 66.6… → 67
        assert adherence.link_compliance(rel) == 67

    def test_only_counts_the_latest_delivered_week(self):
        rel = CoachAthleteFactory()
        now = timezone.now()
        # An older, fully-logged week must not inflate the newest week's number.
        delivered_week(
            rel, sessions=2, done=2, delivered_at=now - timedelta(days=7), index=1
        )
        delivered_week(rel, sessions=2, done=0, delivered_at=now, index=2)
        assert adherence.link_compliance(rel) == 0

    def test_ignores_pending_logs(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=2, done=1)
        # A *pending* log on the second session must not count as done.
        other = week.sessions.order_by("session_slot__day_number").last()
        SessionLogFactory(
            session=other, athlete=rel.athlete, status=SessionLog.Status.PENDING
        )
        assert adherence.link_compliance(rel) == 50

    def test_ignores_another_athletes_logs(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=2, done=0)
        stranger = UserFactory()
        for session in week.sessions.all():
            SessionLogFactory(
                session=session, athlete=stranger, status=SessionLog.Status.DONE
            )
        # The stranger's done logs are not this athlete's adherence.
        assert adherence.link_compliance(rel) == 0

    def test_ignores_archived_plans(self):
        rel = CoachAthleteFactory()
        # The only delivered week lives on an archived plan (e.g. a removed
        # group member's snapshot) — the athlete can't see/log it, so it must
        # not drive the meter.
        delivered_week(rel, sessions=2, done=2, archived=True)
        assert adherence.link_compliance(rel) is None

    def test_prefers_active_plan_over_newer_archived(self):
        rel = CoachAthleteFactory()
        now = timezone.now()
        # A live (older) delivered plan plus a newer *archived* one: the meter
        # measures the live program, not the hidden archived snapshot.
        delivered_week(
            rel, sessions=2, done=1, delivered_at=now - timedelta(days=2), index=1
        )
        delivered_week(
            rel,
            sessions=2,
            done=2,
            delivered_at=now,
            index=2,
            archived=True,
        )
        assert adherence.link_compliance(rel) == 50

    def test_duplicate_done_logs_count_the_session_once(self):
        rel = CoachAthleteFactory()
        week = delivered_week(rel, sessions=2, done=0)
        session = week.sessions.order_by("session_slot__day_number").first()
        # Two done logs for the *same* session (the model allows dated history)
        # must count that session once, never push compliance past 100.
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )
        assert adherence.link_compliance(rel) == 50


# -- recent_logs -----------------------------------------------------------


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


# -- presenters ------------------------------------------------------------


class TestRosterPresenters:
    def test_roster_athlete_threads_compliance(self):
        user = UserFactory()
        row = presenters.roster_athlete(user, compliance=42)
        assert row["compliance"] == 42

    def test_roster_athlete_compliance_defaults_none(self):
        user = UserFactory()
        row = presenters.roster_athlete(user)
        assert row["compliance"] is None

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
    def test_compliance_meter_and_activity_render(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        rel = CoachAthleteFactory(coach=coach)
        rel.athlete.name = "Maya Okonkwo"
        rel.athlete.save(update_fields=["name"])
        week = delivered_week(rel, sessions=2, done=1)
        session = week.sessions.order_by("session_slot__day_number").first()
        session.session_slot.name = "Lower"
        session.session_slot.save(update_fields=["name"])
        # Re-log so the activity text picks up the renamed session.
        SessionLog.objects.filter(session=session, athlete=rel.athlete).delete()
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )

        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        body = resp.content.decode()
        # Compliance meter for the one delivered+half-logged week.
        assert "50%" in body
        # Recent-activity feed names the athlete + the session they logged.
        assert "Lower" in body
