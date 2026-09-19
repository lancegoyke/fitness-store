"""RED tests for #509 slice 2 — the `/meso/analytics/` staff dashboard.

Read first: `docs/meso/decisions.md` "First-party usage events (#509)";
`analytics/{events,models,track}.py`; `meso/views.py`
`UsageDashboardView`/`TourFunnelView`; `notifications/views.py`
`EmailDashboardView._days`; `meso/presenters.py` `tour_funnel`;
`meso/tests/test_tour_funnel.py`.

Pre-implementation this is RED: `presenters.product_analytics` and
`meso_extras.short_duration` don't exist yet (`AttributeError`), and
`meso:product_analytics` has no URL yet (`NoReverseMatch`). Those lookups
happen inside each test body (never at module or class-body scope) so a
missing name fails only the test that needs it, not the whole module.
"""

import datetime
import re
import statistics

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.meso import presenters
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachInviteFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekDeliveryFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.meso.models import PlanAction
from store_project.meso.models import PushSubscription
from store_project.meso.models import SandboxSession
from store_project.meso.models import SessionLog
from store_project.meso.templatetags import meso_extras
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.notifications.models import EmailEvent
from store_project.notifications.models import EmailKind
from store_project.notifications.models import SentEmail
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


@pytest.fixture
def now():
    return timezone.now()


def _relationship(**kwargs):
    kwargs.setdefault("status", CoachAthlete.Status.ACTIVE)
    return CoachAthleteFactory(**kwargs)


def _plan(relationship=None, **kwargs):
    return PlanFactory(relationship=relationship, **kwargs)


def _template_plan(coach):
    return PlanFactory(relationship=None, is_template=True, owner=coach)


def _week_for(plan):
    meso = MesocycleFactory(plan=plan)
    return WeekFactory(mesocycle=meso)


def _session_for(plan, *, week=None):
    if week is None:
        week = _week_for(plan)
    return day(week)


def _logged_set(athlete, plan, when, *, week=None, status=SessionLog.Status.DONE):
    """A ``SessionLog`` with >=1 ``LoggedSet`` on a fresh session of ``plan``."""
    session = _session_for(plan, week=week)
    cell = presc(session)
    log = SessionLogFactory(session=session, athlete=athlete, status=status)
    LoggedSetFactory(session_log=log, prescription=cell)
    SessionLog.objects.filter(pk=log.pk).update(created_at=when)
    log.refresh_from_db()
    return log


def _plan_action(plan, when):
    action = PlanAction.objects.create(
        plan=plan, stack=PlanAction.Stack.UNDO, seq=1, label="edit", snapshot={}
    )
    PlanAction.objects.filter(pk=action.pk).update(created_at=when)
    return action


def _touch_plan_created(plan, when):
    Plan.objects.filter(pk=plan.pk).update(created=when)


def _deliver_week(week, when):
    return WeekDeliveryFactory(week=week, delivered_at=when)


def _agent_batch(plan, coach, when, trigger=AgentProposalBatch.Trigger.MANUAL):
    batch = AgentProposalBatchFactory(plan=plan, coach=coach, trigger=trigger)
    AgentProposalBatch.objects.filter(pk=batch.pk).update(created_at=when)
    return batch


def _invite(coach, when, **kwargs):
    invite = CoachInviteFactory(coach=coach, **kwargs)
    CoachInvite.objects.filter(pk=invite.pk).update(created_at=when)
    invite.refresh_from_db()
    return invite


def _event(name, *, actor=None, subject=None, created, **props):
    return Event.objects.create(
        name=name,
        actor=actor,
        subject_type=subject._meta.label_lower if subject is not None else "",
        subject_id=str(subject.pk) if subject is not None else "",
        created=created,
        props=props,
    )


def _sandbox(**kwargs):
    """A user marked as a throwaway sandbox account (any role)."""
    user = UserFactory(**kwargs)
    SandboxSession.objects.create(
        user=user, expires_at=timezone.now() + datetime.timedelta(hours=1)
    )
    return user


def _self_link(coach):
    return CoachAthlete.add_self(coach)


_seq = iter(range(10**9))


def _sent_email(kind, *, sent_at, user=None, recipient="athlete@example.com"):
    return SentEmail.objects.create(
        ses_message_id=f"ses-{next(_seq)}",
        kind=kind,
        recipient=recipient,
        user=user,
        sent_at=sent_at,
    )


def _email_event(
    sent_email, event_type, *, occurred_at=None, recipient=None, kind=None
):
    return EmailEvent.objects.create(
        sent_email=sent_email,
        event_type=event_type,
        ses_message_id=sent_email.ses_message_id,
        sns_message_id=f"sns-{next(_seq)}",
        recipient=recipient or sent_email.recipient,
        kind=kind or sent_email.kind,
        occurred_at=occurred_at or sent_email.sent_at,
    )


# ---------------------------------------------------------------------------
# presenter shape
# ---------------------------------------------------------------------------


class TestPresenterShape:
    def test_returns_the_documented_top_level_keys(self, now):
        result = presenters.product_analytics(days=30, now=now)

        assert set(result) == {
            "days",
            "since",
            "now",
            "events_since",
            "active_users",
            "funnel",
            "features",
            "email",
        }
        assert result["days"] == 30
        assert result["since"] == now - datetime.timedelta(days=30)
        assert result["now"] == now

    def test_events_since_is_the_earliest_event_ever_recorded(self, now):
        first = now - datetime.timedelta(days=40)
        _event(EventName.PLAN_CREATED, actor=UserFactory(), created=first)
        _event(
            EventName.PLAN_CREATED,
            actor=UserFactory(),
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["events_since"] == first

    def test_events_since_is_none_without_any_events(self, now):
        result = presenters.product_analytics(days=30, now=now)

        assert result["events_since"] is None


# ---------------------------------------------------------------------------
# 1. active users
# ---------------------------------------------------------------------------


class TestActiveCoachSources:
    def test_c1_plan_action_counts_the_coach(self, now):
        coach = UserFactory()
        plan = _plan(_relationship(coach=coach))
        _plan_action(plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1

    def test_c1_template_plan_action_counts_the_owner(self, now):
        coach = UserFactory()
        plan = _template_plan(coach)
        _plan_action(plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1

    def test_c2_plan_created_counts_the_coach(self, now):
        coach = UserFactory()
        plan = _plan(_relationship(coach=coach))
        _touch_plan_created(plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1

    def test_c2_template_plan_created_counts_the_owner(self, now):
        coach = UserFactory()
        plan = _template_plan(coach)
        _touch_plan_created(plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1

    def test_c3_week_delivery_counts_the_coach(self, now):
        coach = UserFactory()
        plan = _plan(_relationship(coach=coach))
        week = _week_for(plan)
        _deliver_week(week, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1

    def test_c4_agent_proposal_batch_counts_the_coach(self, now):
        coach = UserFactory()
        plan = _plan(_relationship(coach=coach))
        _agent_batch(
            plan,
            coach,
            now - datetime.timedelta(days=1),
            trigger=AgentProposalBatch.Trigger.MANUAL,
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1

    def test_c4_eval_trigger_does_not_count(self, now):
        coach = UserFactory()
        plan = _plan(_relationship(coach=coach))
        _agent_batch(
            plan,
            coach,
            now - datetime.timedelta(days=1),
            trigger=AgentProposalBatch.Trigger.EVAL,
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 0

    def test_c5_coach_invite_counts_the_coach(self, now):
        coach = UserFactory()
        _invite(coach, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1

    def test_c6_event_counts_the_coach(self, now):
        coach = UserFactory()
        _event(
            EventName.PLAN_CREATED,
            actor=coach,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1


class TestActiveAthleteSources:
    def test_a1_logged_set_counts_the_athlete(self, now):
        athlete = UserFactory()
        plan = _plan(_relationship(athlete=athlete))
        _logged_set(athlete, plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 1

    def test_a1_session_log_without_any_logged_set_does_not_count(self, now):
        athlete = UserFactory()
        plan = _plan(_relationship(athlete=athlete))
        session = _session_for(plan)
        log = SessionLogFactory(session=session, athlete=athlete)
        SessionLog.objects.filter(pk=log.pk).update(
            created_at=now - datetime.timedelta(days=1)
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 0

    def test_a2_set_logged_event_counts_the_athlete(self, now):
        athlete = UserFactory()
        plan = _plan(_relationship(athlete=athlete))
        session = _session_for(plan)
        log = SessionLogFactory(session=session, athlete=athlete)
        _event(
            EventName.SET_LOGGED,
            actor=athlete,
            subject=log,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 1

    def test_a2_session_opened_event_counts_the_athlete(self, now):
        athlete = UserFactory()
        plan = _plan(_relationship(athlete=athlete))
        session = _session_for(plan)
        _event(
            EventName.SESSION_OPENED,
            actor=athlete,
            subject=session,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 1


class TestActiveUsersWindows:
    def test_activity_10_days_ago_is_mau_and_window_not_wau(self, now):
        coach = UserFactory()
        plan = _plan(_relationship(coach=coach))
        _touch_plan_created(plan, now - datetime.timedelta(days=10))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["wau"] == 0
        assert result["active_users"]["coaches"]["mau"] == 1
        assert result["active_users"]["coaches"]["window"] == 1

    def test_activity_31_days_ago_is_outside_30_but_inside_90(self, now):
        coach = UserFactory()
        plan = _plan(_relationship(coach=coach))
        _touch_plan_created(plan, now - datetime.timedelta(days=31))

        result_30 = presenters.product_analytics(days=30, now=now)
        result_90 = presenters.product_analytics(days=90, now=now)

        assert result_30["active_users"]["coaches"]["window"] == 0
        assert result_30["active_users"]["coaches"]["mau"] == 0
        assert result_90["active_users"]["coaches"]["window"] == 1

    def test_activity_29_days_ago_is_inside_a_30_day_window(self, now):
        coach = UserFactory()
        plan = _plan(_relationship(coach=coach))
        _touch_plan_created(plan, now - datetime.timedelta(days=29))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1

    def test_coach_active_via_several_sources_counts_once(self, now):
        coach = UserFactory()
        plan = _plan(_relationship(coach=coach))
        _touch_plan_created(plan, now - datetime.timedelta(days=1))
        _plan_action(plan, now - datetime.timedelta(days=1))
        _event(
            EventName.PLAN_CREATED,
            actor=coach,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1

    def test_a_user_can_be_active_as_both_coach_and_athlete(self, now):
        person = UserFactory()
        own_plan = _plan(_relationship(coach=person))
        _touch_plan_created(own_plan, now - datetime.timedelta(days=1))
        other_plan = _plan(_relationship(athlete=person))
        _logged_set(person, other_plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1
        assert result["active_users"]["athletes"]["window"] == 1


class TestIneligibleUsersExcludedFromActive:
    def test_staff_coach_activity_excluded(self, now):
        coach = UserFactory(is_staff=True)
        plan = _plan(_relationship(coach=coach))
        _touch_plan_created(plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 0

    def test_sandbox_coach_activity_excluded(self, now):
        coach = _sandbox()
        plan = _plan(_relationship(coach=coach))
        _touch_plan_created(plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 0

    def test_staff_athlete_activity_excluded(self, now):
        athlete = UserFactory(is_staff=True)
        plan = _plan(_relationship(athlete=athlete))
        _logged_set(athlete, plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 0

    def test_sandbox_athlete_activity_excluded(self, now):
        athlete = _sandbox()
        plan = _plan(_relationship(athlete=athlete))
        _logged_set(athlete, plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 0


class TestDemoExclusionFromActive:
    def test_demo_relationship_plan_created_does_not_count(self, now):
        coach = UserFactory()
        rel = _relationship(coach=coach, is_demo=True)
        plan = _plan(rel)
        _touch_plan_created(plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 0

    def test_demo_relationship_delivery_does_not_count(self, now):
        coach = UserFactory()
        rel = _relationship(coach=coach, is_demo=True)
        plan = _plan(rel)
        week = _week_for(plan)
        _deliver_week(week, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 0

    def test_demo_relationship_agent_run_does_not_count(self, now):
        coach = UserFactory()
        rel = _relationship(coach=coach, is_demo=True)
        plan = _plan(rel)
        _agent_batch(plan, coach, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 0

    def test_demo_subject_event_does_not_count(self, now):
        coach = UserFactory()
        rel = _relationship(coach=coach, is_demo=True)
        plan = _plan(rel)
        _event(
            EventName.PLAN_CREATED,
            actor=coach,
            subject=plan,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 0


class TestSelfCoachingActive:
    def test_self_plan_logged_set_does_not_count_as_athlete_activity(self, now):
        coach = UserFactory()
        rel = _self_link(coach)
        plan = _plan(rel)
        _logged_set(coach, plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 0

    def test_self_plan_set_logged_event_does_not_count_as_athlete_activity(self, now):
        coach = UserFactory()
        rel = _self_link(coach)
        plan = _plan(rel)
        session = _session_for(plan)
        _event(
            EventName.SET_LOGGED,
            actor=coach,
            subject=session,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 0

    def test_self_link_coach_plan_edits_still_count_as_coach_activity(self, now):
        coach = UserFactory()
        rel = _self_link(coach)
        plan = _plan(rel)
        _touch_plan_created(plan, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1


# ---------------------------------------------------------------------------
# 2. activation funnel
# ---------------------------------------------------------------------------


def _paths(result):
    return {row["key"]: row for row in result["funnel"]}


class TestFunnelShape:
    def test_funnel_has_the_three_paths_in_order_with_labels(self, now):
        result = presenters.product_analytics(days=30, now=now)

        assert [row["key"] for row in result["funnel"]] == [
            "email_invite",
            "athlete_request",
            "all",
        ]
        labels = {row["key"]: row["label"] for row in result["funnel"]}
        assert labels == {
            "email_invite": "Email invite",
            "athlete_request": "Athlete request",
            "all": "All",
        }


class TestEmailInviteFunnel:
    def test_sent_only_invite_counts_as_sent(self, now):
        coach = UserFactory()
        _invite(coach, now - datetime.timedelta(days=5))

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["email_invite"]

        assert path["sent"] == 1
        assert path["accepted"] == 0
        assert path["delivered"] == 0
        assert path["logged"] == 0
        assert path["median_to_accept"] is None

    def test_accepted_invite_counts_through_accepted(self, now):
        coach = UserFactory()
        athlete = UserFactory()
        sent_at = now - datetime.timedelta(days=10)
        accepted_at = sent_at + datetime.timedelta(hours=3)
        rel = _relationship(coach=coach, athlete=athlete)
        invite = CoachInviteFactory(
            coach=coach,
            accepted_by=athlete,
            accepted_link=rel,
            status=CoachInvite.Status.ACCEPTED,
        )
        CoachInvite.objects.filter(pk=invite.pk).update(
            created_at=sent_at, responded_at=accepted_at
        )

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["email_invite"]

        assert path["sent"] == 1
        assert path["accepted"] == 1
        assert path["delivered"] == 0
        assert path["logged"] == 0
        assert path["median_to_accept"] == accepted_at - sent_at

    def test_accepted_and_delivered_invite_counts_through_delivered(self, now):
        coach = UserFactory()
        athlete = UserFactory()
        sent_at = now - datetime.timedelta(days=10)
        accepted_at = sent_at + datetime.timedelta(hours=3)
        delivered_at = accepted_at + datetime.timedelta(days=1)
        rel = _relationship(coach=coach, athlete=athlete)
        invite = CoachInviteFactory(
            coach=coach,
            accepted_by=athlete,
            accepted_link=rel,
            status=CoachInvite.Status.ACCEPTED,
        )
        CoachInvite.objects.filter(pk=invite.pk).update(
            created_at=sent_at, responded_at=accepted_at
        )
        plan = _plan(rel)
        week = _week_for(plan)
        _deliver_week(week, delivered_at)

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["email_invite"]

        assert path["accepted"] == 1
        assert path["delivered"] == 1
        assert path["logged"] == 0
        assert path["median_to_deliver"] == delivered_at - accepted_at

    def test_full_chain_counts_through_logged(self, now):
        coach = UserFactory()
        athlete = UserFactory()
        sent_at = now - datetime.timedelta(days=10)
        accepted_at = sent_at + datetime.timedelta(hours=3)
        delivered_at = accepted_at + datetime.timedelta(days=1)
        logged_at = delivered_at + datetime.timedelta(hours=5)
        rel = _relationship(coach=coach, athlete=athlete)
        invite = CoachInviteFactory(
            coach=coach,
            accepted_by=athlete,
            accepted_link=rel,
            status=CoachInvite.Status.ACCEPTED,
        )
        CoachInvite.objects.filter(pk=invite.pk).update(
            created_at=sent_at, responded_at=accepted_at
        )
        plan = _plan(rel)
        week = _week_for(plan)
        _deliver_week(week, delivered_at)
        _logged_set(athlete, plan, logged_at, week=week)

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["email_invite"]

        assert path["delivered"] == 1
        assert path["logged"] == 1
        assert path["median_to_log"] == logged_at - delivered_at

    def test_exact_medians_with_an_even_count(self, now):
        """4 accepted invites pins the mean-of-middle-two rule."""
        sent_at = now - datetime.timedelta(days=10)
        deltas = [datetime.timedelta(hours=h) for h in (1, 2, 3, 4)]
        for delta in deltas:
            coach = UserFactory()
            athlete = UserFactory()
            rel = _relationship(coach=coach, athlete=athlete)
            invite = CoachInviteFactory(
                coach=coach,
                accepted_by=athlete,
                accepted_link=rel,
                status=CoachInvite.Status.ACCEPTED,
            )
            CoachInvite.objects.filter(pk=invite.pk).update(
                created_at=sent_at, responded_at=sent_at + delta
            )

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["email_invite"]

        assert path["accepted"] == 4
        assert path["median_to_accept"] == statistics.median(deltas)

    def test_invite_31_days_old_is_outside_a_30_day_window(self, now):
        coach = UserFactory()
        _invite(coach, now - datetime.timedelta(days=31))

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["email_invite"]

        assert path["sent"] == 0

    def test_set_logged_before_the_first_delivery_does_not_count(self, now):
        coach = UserFactory()
        athlete = UserFactory()
        sent_at = now - datetime.timedelta(days=10)
        accepted_at = sent_at + datetime.timedelta(hours=1)
        delivered_at = accepted_at + datetime.timedelta(days=2)
        early_log_at = accepted_at + datetime.timedelta(hours=6)
        rel = _relationship(coach=coach, athlete=athlete)
        invite = CoachInviteFactory(
            coach=coach,
            accepted_by=athlete,
            accepted_link=rel,
            status=CoachInvite.Status.ACCEPTED,
        )
        CoachInvite.objects.filter(pk=invite.pk).update(
            created_at=sent_at, responded_at=accepted_at
        )
        plan = _plan(rel)
        week = _week_for(plan)
        _deliver_week(week, delivered_at)
        _logged_set(athlete, plan, early_log_at, week=week)

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["email_invite"]

        assert path["delivered"] == 1
        assert path["logged"] == 0

    def test_delivery_on_a_different_relationship_does_not_count(self, now):
        coach = UserFactory()
        athlete = UserFactory()
        sent_at = now - datetime.timedelta(days=10)
        accepted_at = sent_at + datetime.timedelta(hours=1)
        rel = _relationship(coach=coach, athlete=athlete)
        invite = CoachInviteFactory(
            coach=coach,
            accepted_by=athlete,
            accepted_link=rel,
            status=CoachInvite.Status.ACCEPTED,
        )
        CoachInvite.objects.filter(pk=invite.pk).update(
            created_at=sent_at, responded_at=accepted_at
        )
        other_rel = _relationship(coach=coach)
        other_plan = _plan(other_rel)
        other_week = _week_for(other_plan)
        _deliver_week(other_week, accepted_at + datetime.timedelta(hours=2))

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["email_invite"]

        assert path["delivered"] == 0

    def test_staff_and_sandbox_coach_invites_excluded(self, now):
        staff_coach = UserFactory(is_staff=True)
        _invite(staff_coach, now - datetime.timedelta(days=1))
        sandbox_coach = _sandbox()
        _invite(sandbox_coach, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["email_invite"]

        assert path["sent"] == 0

    def test_sandbox_accepted_by_excludes_the_invite(self, now):
        coach = UserFactory()
        sandbox_athlete = _sandbox()
        rel = _relationship(coach=coach, athlete=sandbox_athlete)
        invite = CoachInviteFactory(
            coach=coach,
            accepted_by=sandbox_athlete,
            accepted_link=rel,
            status=CoachInvite.Status.ACCEPTED,
        )
        CoachInvite.objects.filter(pk=invite.pk).update(
            created_at=now - datetime.timedelta(days=1)
        )

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["email_invite"]

        assert path["sent"] == 0


class TestAthleteRequestFunnel:
    def test_full_chain_counts_under_athlete_request_and_all(self, now):
        coach = UserFactory()
        athlete = UserFactory()
        sent_at = now - datetime.timedelta(days=10)
        accepted_at = sent_at + datetime.timedelta(hours=2)
        delivered_at = accepted_at + datetime.timedelta(days=1)
        logged_at = delivered_at + datetime.timedelta(hours=4)
        link = CoachAthleteFactory(
            coach=coach,
            athlete=athlete,
            invited_by=CoachAthlete.InvitedBy.ATHLETE,
            status=CoachAthlete.Status.ACTIVE,
        )
        CoachAthlete.objects.filter(pk=link.pk).update(
            created_at=sent_at, responded_at=accepted_at
        )
        plan = _plan(link)
        week = _week_for(plan)
        _deliver_week(week, delivered_at)
        _logged_set(athlete, plan, logged_at, week=week)

        result = presenters.product_analytics(days=30, now=now)
        paths = _paths(result)

        assert paths["athlete_request"]["sent"] == 1
        assert paths["athlete_request"]["accepted"] == 1
        assert paths["athlete_request"]["delivered"] == 1
        assert paths["athlete_request"]["logged"] == 1
        assert paths["all"]["sent"] == 1
        assert paths["all"]["logged"] == 1

    def test_request_link_claimed_by_an_invite_is_not_double_counted(self, now):
        coach = UserFactory()
        athlete = UserFactory()
        sent_at = now - datetime.timedelta(days=5)
        link = CoachAthleteFactory(
            coach=coach,
            athlete=athlete,
            invited_by=CoachAthlete.InvitedBy.ATHLETE,
            status=CoachAthlete.Status.ACTIVE,
        )
        CoachAthlete.objects.filter(pk=link.pk).update(
            created_at=sent_at, responded_at=sent_at + datetime.timedelta(hours=1)
        )
        invite = CoachInviteFactory(
            coach=coach,
            accepted_by=athlete,
            accepted_link=link,
            status=CoachInvite.Status.ACCEPTED,
        )
        CoachInvite.objects.filter(pk=invite.pk).update(
            created_at=sent_at, responded_at=sent_at + datetime.timedelta(hours=1)
        )

        result = presenters.product_analytics(days=30, now=now)
        paths = _paths(result)

        assert paths["athlete_request"]["sent"] == 0
        assert paths["email_invite"]["sent"] == 1

    def test_relationship_reinvite_keeps_its_original_created_at_out_of_window(
        self, now
    ):
        """A reopened row keeps its original ``created_at``.

        No send time, so a long-ago reinvite doesn't newly enter the funnel.
        """
        coach = UserFactory()
        athlete = UserFactory()
        old_sent_at = now - datetime.timedelta(days=100)
        link = CoachAthleteFactory(
            coach=coach,
            athlete=athlete,
            invited_by=CoachAthlete.InvitedBy.ATHLETE,
            status=CoachAthlete.Status.ENDED,
        )
        CoachAthlete.objects.filter(pk=link.pk).update(created_at=old_sent_at)
        link.status = CoachAthlete.Status.ACTIVE
        link.responded_at = now - datetime.timedelta(days=1)
        link.save(update_fields=["status", "responded_at"])

        result = presenters.product_analytics(days=30, now=now)
        path = _paths(result)["athlete_request"]

        assert path["sent"] == 0


def _accepted_invite(coach, athlete, sent_at, accepted_after):
    rel = _relationship(coach=coach, athlete=athlete)
    invite = CoachInviteFactory(
        coach=coach,
        accepted_by=athlete,
        accepted_link=rel,
        status=CoachInvite.Status.ACCEPTED,
    )
    CoachInvite.objects.filter(pk=invite.pk).update(
        created_at=sent_at, responded_at=sent_at + accepted_after
    )
    return rel


class TestMixedCohortFunnel:
    def test_every_step_with_one_stopping_at_each_and_medians_per_step(self, now):
        """Email invites stop at each step in turn; one request runs the chain.

        email:   A sent only; B accepted 1h; C accepted 3h, delivered +1d;
                 D accepted 5h, delivered +3d, logged +2h.
        request: E accepted 7h, delivered +2d, logged +4h.
        """
        hour = datetime.timedelta(hours=1)
        dayd = datetime.timedelta(days=1)
        sent_at = now - datetime.timedelta(days=20)

        _invite(UserFactory(), sent_at)  # A
        _accepted_invite(UserFactory(), UserFactory(), sent_at, 1 * hour)  # B
        rel_c = _accepted_invite(UserFactory(), UserFactory(), sent_at, 3 * hour)
        _deliver_week(_week_for(_plan(rel_c)), sent_at + 3 * hour + dayd)
        athlete_d = UserFactory()
        rel_d = _accepted_invite(UserFactory(), athlete_d, sent_at, 5 * hour)
        plan_d = _plan(rel_d)
        week_d = _week_for(plan_d)
        delivered_d = sent_at + 5 * hour + 3 * dayd
        _deliver_week(week_d, delivered_d)
        _logged_set(athlete_d, plan_d, delivered_d + 2 * hour, week=week_d)

        athlete_e = UserFactory()
        link_e = CoachAthleteFactory(
            coach=UserFactory(),
            athlete=athlete_e,
            invited_by=CoachAthlete.InvitedBy.ATHLETE,
            status=CoachAthlete.Status.ACTIVE,
        )
        CoachAthlete.objects.filter(pk=link_e.pk).update(
            created_at=sent_at, responded_at=sent_at + 7 * hour
        )
        plan_e = _plan(link_e)
        week_e = _week_for(plan_e)
        delivered_e = sent_at + 7 * hour + 2 * dayd
        _deliver_week(week_e, delivered_e)
        _logged_set(athlete_e, plan_e, delivered_e + 4 * hour, week=week_e)

        paths = _paths(presenters.product_analytics(days=30, now=now))

        email = paths["email_invite"]
        assert (email["sent"], email["accepted"]) == (4, 3)
        assert (email["delivered"], email["logged"]) == (2, 1)
        assert email["median_to_accept"] == 3 * hour
        assert email["median_to_deliver"] == 2 * dayd
        assert email["median_to_log"] == 2 * hour

        request = paths["athlete_request"]
        assert (request["sent"], request["accepted"]) == (1, 1)
        assert (request["delivered"], request["logged"]) == (1, 1)

        both = paths["all"]
        assert (both["sent"], both["accepted"]) == (5, 4)
        assert (both["delivered"], both["logged"]) == (3, 2)
        assert both["median_to_accept"] == 4 * hour  # median(1, 3, 5, 7 h)
        assert both["median_to_deliver"] == 2 * dayd  # median(1, 3, 2 d)
        assert both["median_to_log"] == 3 * hour  # median(2, 4 h)


# ---------------------------------------------------------------------------
# 3. feature adoption
# ---------------------------------------------------------------------------


def _feature(result, key):
    return next(row for row in result["features"] if row["key"] == key)


class TestFeatureAdoption:
    def test_features_are_returned_in_the_documented_order(self, now):
        result = presenters.product_analytics(days=30, now=now)

        assert [row["key"] for row in result["features"]] == [
            "plan_created",
            "agent_draft",
            "agent_run",
            "batch_applied",
            "template_imported",
            "block_delivered",
            "invite_sent",
            "trial_started",
            "subscription_started",
            "subscription_cancelled",
            "push_enabled",
            "session_completed",
        ]

    def test_plan_created_users_and_times(self, now):
        actor1, actor2 = UserFactory(), UserFactory()
        plan = PlanFactory()
        _event(
            EventName.PLAN_CREATED,
            actor=actor1,
            subject=plan,
            created=now - datetime.timedelta(days=1),
        )
        _event(
            EventName.PLAN_CREATED,
            actor=actor1,
            subject=plan,
            created=now - datetime.timedelta(days=2),
        )
        _event(
            EventName.PLAN_CREATED,
            actor=actor2,
            subject=plan,
            created=now - datetime.timedelta(days=3),
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "plan_created")

        assert row["label"] == "New program"
        assert row["who"] == "coaches"
        assert row["users"] == 2
        assert row["times"] == 3

    def test_agent_draft_counts_draft_trigger_batches(self, now):
        coach = UserFactory()
        plan = PlanFactory(relationship__coach=coach)
        _agent_batch(
            plan,
            coach,
            now - datetime.timedelta(days=1),
            trigger=AgentProposalBatch.Trigger.DRAFT,
        )
        _agent_batch(
            plan,
            coach,
            now - datetime.timedelta(days=2),
            trigger=AgentProposalBatch.Trigger.DRAFT,
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "agent_draft")

        assert row["who"] == "coaches"
        assert row["users"] == 1
        assert row["times"] == 2

    def test_agent_run_counts_manual_trigger_batches_only(self, now):
        coach = UserFactory()
        plan = PlanFactory(relationship__coach=coach)
        _agent_batch(
            plan,
            coach,
            now - datetime.timedelta(days=1),
            trigger=AgentProposalBatch.Trigger.MANUAL,
        )
        _agent_batch(
            plan,
            coach,
            now - datetime.timedelta(days=1),
            trigger=AgentProposalBatch.Trigger.DRAFT,
        )
        _agent_batch(
            plan,
            coach,
            now - datetime.timedelta(days=1),
            trigger=AgentProposalBatch.Trigger.EVAL,
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "agent_run")

        assert row["times"] == 1

    def test_batch_applied_event(self, now):
        actor = UserFactory()
        _event(
            EventName.BATCH_APPLIED,
            actor=actor,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "batch_applied")

        assert row["users"] == 1
        assert row["times"] == 1

    def test_template_imported_event(self, now):
        actor = UserFactory()
        _event(
            EventName.TEMPLATE_IMPORTED,
            actor=actor,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "template_imported")

        assert row["users"] == 1
        assert row["times"] == 1

    def test_block_delivered_three_week_block_counts_as_one_time(self, now):
        coach = UserFactory()
        plan = PlanFactory(relationship__coach=coach)
        meso = MesocycleFactory(plan=plan)
        weeks = [WeekFactory(mesocycle=meso) for _ in range(3)]
        delivered_at = now - datetime.timedelta(days=1)
        for week in weeks:
            _deliver_week(week, delivered_at)

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "block_delivered")

        assert row["who"] == "coaches"
        assert row["users"] == 1
        assert row["times"] == 1

    def test_block_delivered_two_separate_deliveries_count_as_two_times(self, now):
        coach = UserFactory()
        plan = PlanFactory(relationship__coach=coach)
        meso = MesocycleFactory(plan=plan)
        week = WeekFactory(mesocycle=meso)
        _deliver_week(week, now - datetime.timedelta(days=5))
        _deliver_week(week, now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "block_delivered")

        assert row["times"] == 2

    def test_invite_sent(self, now):
        coach = UserFactory()
        _invite(coach, now - datetime.timedelta(days=1))
        _invite(coach, now - datetime.timedelta(days=2))

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "invite_sent")

        assert row["users"] == 1
        assert row["times"] == 2

    def test_trial_started_inside_window(self, now):
        coach = UserFactory()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=now,
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "trial_started")

        assert row["users"] == 1
        assert row["times"] == 1

    def test_trial_started_outside_window(self, now):
        coach = UserFactory()
        # trial_end - 14d must land in [now-30d, now]; this puts the start
        # moment 1h past "now", just outside.
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=now + datetime.timedelta(days=14, hours=1),
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "trial_started")

        assert row["times"] == 0

    def test_subscription_started_via_stripe_counted(self, now):
        actor = UserFactory()
        _event(
            EventName.SUBSCRIPTION_STARTED,
            actor=actor,
            created=now - datetime.timedelta(days=1),
            via="stripe",
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "subscription_started")

        assert row["users"] == 1
        assert row["times"] == 1

    def test_subscription_started_via_trial_not_counted(self, now):
        actor = UserFactory()
        _event(
            EventName.SUBSCRIPTION_STARTED,
            actor=actor,
            created=now - datetime.timedelta(days=1),
            via="trial",
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "subscription_started")

        assert row["times"] == 0

    def test_subscription_cancelled(self, now):
        actor = UserFactory()
        _event(
            EventName.SUBSCRIPTION_CANCELLED,
            actor=actor,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "subscription_cancelled")

        assert row["users"] == 1
        assert row["times"] == 1

    def test_push_enabled(self, now):
        athlete = UserFactory()
        _relationship(athlete=athlete)  # a coach's client
        sub = PushSubscription.objects.create(
            athlete=athlete,
            endpoint="https://push.example/1",
            p256dh="key",
            auth="secret",
        )
        PushSubscription.objects.filter(pk=sub.pk).update(
            created_at=now - datetime.timedelta(days=1)
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "push_enabled")

        assert row["who"] == "athletes"
        assert row["users"] == 1
        assert row["times"] == 1

    def test_session_completed(self, now):
        athlete = UserFactory()
        _relationship(athlete=athlete)  # a coach's client
        _event(
            EventName.SESSION_COMPLETED,
            actor=athlete,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "session_completed")

        assert row["who"] == "athletes"
        assert row["users"] == 1
        assert row["times"] == 1

    def test_demo_plan_created_event_excluded(self, now):
        coach = UserFactory()
        rel = _relationship(coach=coach, is_demo=True)
        plan = _plan(rel)
        _event(
            EventName.PLAN_CREATED,
            actor=coach,
            subject=plan,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "plan_created")

        assert row["times"] == 0

    def test_staff_and_sandbox_actors_excluded(self, now):
        staff = UserFactory(is_staff=True)
        sandbox_user = _sandbox()
        _event(
            EventName.BATCH_APPLIED,
            actor=staff,
            created=now - datetime.timedelta(days=1),
        )
        _event(
            EventName.BATCH_APPLIED,
            actor=sandbox_user,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "batch_applied")

        assert row["times"] == 0

    def test_window_edge_30_days_ago_is_inside_31_days_ago_is_outside(self, now):
        actor_inside = UserFactory()
        actor_outside = UserFactory()
        _event(
            EventName.BATCH_APPLIED,
            actor=actor_inside,
            created=now - datetime.timedelta(days=30),
        )
        _event(
            EventName.BATCH_APPLIED,
            actor=actor_outside,
            created=now - datetime.timedelta(days=30, seconds=1),
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _feature(result, "batch_applied")

        assert row["times"] == 1


# ---------------------------------------------------------------------------
# 4. email
# ---------------------------------------------------------------------------


def _email_row(result, kind):
    return next(r for r in result["email"]["rows"] if r["kind"] == kind)


class TestEmailSection:
    def test_per_kind_sent_delivered_opened_clicked_as_distinct_messages(self, now):
        sent_at = now - datetime.timedelta(days=1)
        msg = _sent_email(EmailKind.BLOCK_DELIVERED, sent_at=sent_at)
        _email_event(msg, EmailEvent.EventType.DELIVERY, occurred_at=sent_at)
        _email_event(
            msg,
            EmailEvent.EventType.OPEN,
            occurred_at=sent_at + datetime.timedelta(minutes=5),
        )
        _email_event(
            msg,
            EmailEvent.EventType.OPEN,
            occurred_at=sent_at + datetime.timedelta(minutes=10),
        )
        _email_event(
            msg,
            EmailEvent.EventType.CLICK,
            occurred_at=sent_at + datetime.timedelta(minutes=15),
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _email_row(result, EmailKind.BLOCK_DELIVERED)

        assert row["sent"] == 1
        assert row["delivered"] == 1
        assert row["opened"] == 1  # two opens on one message = one opened message
        assert row["clicked"] == 1
        assert row["open_rate"] == 100
        assert row["click_rate"] == 100

    def test_every_meso_kind_present_and_zero_filled(self, now):
        result = presenters.product_analytics(days=30, now=now)
        kinds = [r["kind"] for r in result["email"]["rows"]]

        assert kinds == [
            EmailKind.BLOCK_DELIVERED,
            EmailKind.COACH_INVITE,
            EmailKind.INVITE_REMINDER,
            EmailKind.COACH_REQUEST,
        ]
        for row in result["email"]["rows"]:
            assert row["sent"] == 0
            assert row["open_rate"] is None
            assert row["click_rate"] is None

    def test_non_meso_kind_absent_and_not_counted(self, now):
        _sent_email(EmailKind.PASSWORD_RESET, sent_at=now - datetime.timedelta(days=1))

        result = presenters.product_analytics(days=30, now=now)
        kinds = [r["kind"] for r in result["email"]["rows"]]

        assert EmailKind.PASSWORD_RESET not in kinds
        assert result["email"]["totals"]["sent"] == 0

    def test_staff_recipient_excluded(self, now):
        staff = UserFactory(is_staff=True)
        _sent_email(
            EmailKind.COACH_INVITE, sent_at=now - datetime.timedelta(days=1), user=staff
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _email_row(result, EmailKind.COACH_INVITE)

        assert row["sent"] == 0

    def test_null_user_send_is_kept(self, now):
        _sent_email(
            EmailKind.COACH_INVITE, sent_at=now - datetime.timedelta(days=1), user=None
        )

        result = presenters.product_analytics(days=30, now=now)
        row = _email_row(result, EmailKind.COACH_INVITE)

        assert row["sent"] == 1

    def test_message_31_days_ago_is_excluded(self, now):
        _sent_email(EmailKind.COACH_INVITE, sent_at=now - datetime.timedelta(days=31))

        result = presenters.product_analytics(days=30, now=now)
        row = _email_row(result, EmailKind.COACH_INVITE)

        assert row["sent"] == 0

    def test_rate_is_rounded_and_none_when_no_sends(self, now):
        sent_at = now - datetime.timedelta(days=1)
        for _ in range(3):
            _sent_email(EmailKind.COACH_REQUEST, sent_at=sent_at)
        opened_msg = SentEmail.objects.filter(kind=EmailKind.COACH_REQUEST).first()
        _email_event(opened_msg, EmailEvent.EventType.OPEN, occurred_at=sent_at)

        result = presenters.product_analytics(days=30, now=now)
        row = _email_row(result, EmailKind.COACH_REQUEST)

        assert row["sent"] == 3
        assert row["opened"] == 1
        assert row["open_rate"] == 33  # round(100 * 1/3)
        empty_row = _email_row(result, EmailKind.INVITE_REMINDER)
        assert empty_row["sent"] == 0
        assert empty_row["open_rate"] is None

    def test_totals_sum_across_kinds_and_recompute_rates(self, now):
        sent_at = now - datetime.timedelta(days=1)
        msg1 = _sent_email(EmailKind.BLOCK_DELIVERED, sent_at=sent_at)
        _email_event(msg1, EmailEvent.EventType.OPEN, occurred_at=sent_at)
        _sent_email(EmailKind.COACH_INVITE, sent_at=sent_at)
        _sent_email(EmailKind.COACH_INVITE, sent_at=sent_at)

        result = presenters.product_analytics(days=30, now=now)
        totals = result["email"]["totals"]

        assert totals["sent"] == 3
        assert totals["opened"] == 1
        assert "kind" not in totals
        assert "label" not in totals
        assert totals["open_rate"] == 33  # round(100 * 1/3)


# ---------------------------------------------------------------------------
# short_duration filter
# ---------------------------------------------------------------------------


class TestShortDurationFilter:
    def test_none_renders_as_an_em_dash(self):
        assert meso_extras.short_duration(None) == "—"

    def test_under_one_hour_renders_in_minutes(self):
        assert meso_extras.short_duration(datetime.timedelta(minutes=45)) == "45 min"

    def test_under_48_hours_renders_in_hours(self):
        assert meso_extras.short_duration(datetime.timedelta(hours=30)) == "30 h"

    def test_at_exactly_one_hour_renders_in_hours_not_minutes(self):
        assert meso_extras.short_duration(datetime.timedelta(hours=1)) == "1 h"

    def test_at_least_48_hours_renders_in_tenths_of_a_day(self):
        assert (
            meso_extras.short_duration(datetime.timedelta(days=3, hours=12)) == "3.5 d"
        )

    def test_at_exactly_48_hours_renders_in_days_not_hours(self):
        assert meso_extras.short_duration(datetime.timedelta(hours=48)) == "2.0 d"


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------


class TestExclusionsSurviveDeletedSubjects:
    """Exclusions that outlive the subject row (adversarial review, round 1).

    The subject-based exclusions only match rows that still exist. "Remove demo
    data" deletes demo plans, and clearing a typed line reaps an empty log, but
    the events stay.
    """

    def test_demo_flagged_event_counts_nowhere_after_the_demo_is_removed(self, now):
        coach = UserFactory()
        event = _event(
            EventName.PLAN_CREATED,
            actor=coach,
            created=now - datetime.timedelta(days=1),
            demo=True,
        )
        # The demo plan it pointed at is gone (clear_demo cascades).
        Event.objects.filter(pk=event.pk).update(
            subject_type="meso.plan", subject_id="999999"
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 0
        assert _feature(result, "plan_created")["times"] == 0

    def test_non_demo_event_still_counts(self, now):
        coach = UserFactory()
        _event(
            EventName.PLAN_CREATED,
            actor=coach,
            created=now - datetime.timedelta(days=1),
            demo=False,
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["coaches"]["window"] == 1
        assert _feature(result, "plan_created")["times"] == 1

    def test_self_only_coach_set_logged_on_a_deleted_log_is_not_an_athlete(self, now):
        coach = UserFactory()
        _self_link(coach)
        event = _event(
            EventName.SET_LOGGED, actor=coach, created=now - datetime.timedelta(days=1)
        )
        Event.objects.filter(pk=event.pk).update(
            subject_type="meso.sessionlog", subject_id="999999"
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 0

    def test_client_athlete_set_logged_on_a_deleted_log_still_counts(self, now):
        athlete = UserFactory()
        _relationship(athlete=athlete)
        event = _event(
            EventName.SET_LOGGED,
            actor=athlete,
            created=now - datetime.timedelta(days=1),
        )
        Event.objects.filter(pk=event.pk).update(
            subject_type="meso.sessionlog", subject_id="999999"
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 1

    def test_session_completed_on_a_self_plan_is_not_an_athlete_row(self, now):
        coach = UserFactory()
        link = _self_link(coach)
        log = _logged_set(coach, _plan(link), now - datetime.timedelta(days=1))
        _event(
            EventName.SESSION_COMPLETED,
            actor=coach,
            subject=log,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)

        assert _feature(result, "session_completed")["users"] == 0
        assert _feature(result, "session_completed")["times"] == 0

    def test_push_enabled_by_a_self_only_coach_is_not_an_athlete_row(self, now):
        coach = UserFactory()
        _self_link(coach)
        sub = PushSubscription.objects.create(
            athlete=coach, endpoint="https://push.example/self", p256dh="k", auth="a"
        )
        PushSubscription.objects.filter(pk=sub.pk).update(
            created_at=now - datetime.timedelta(days=1)
        )

        result = presenters.product_analytics(days=30, now=now)

        assert _feature(result, "push_enabled")["users"] == 0


class TestClientAthletesNeedAnAnsweredLink:
    """Adversarial review round 2: a request nobody accepted isn't coaching."""

    @pytest.mark.parametrize(
        "status",
        [CoachAthlete.Status.PENDING_ATHLETE_REQUEST, CoachAthlete.Status.DECLINED],
    )
    def test_self_coach_with_an_unaccepted_request_is_not_an_athlete(self, now, status):
        user = UserFactory()
        _self_link(user)
        CoachAthleteFactory(
            coach=UserFactory(),
            athlete=user,
            invited_by=CoachAthlete.InvitedBy.ATHLETE,
            status=status,
        )
        sub = PushSubscription.objects.create(
            athlete=user, endpoint="https://push.example/u", p256dh="k", auth="a"
        )
        PushSubscription.objects.filter(pk=sub.pk).update(
            created_at=now - datetime.timedelta(days=1)
        )
        event = _event(
            EventName.SET_LOGGED, actor=user, created=now - datetime.timedelta(days=1)
        )
        Event.objects.filter(pk=event.pk).update(
            subject_type="meso.sessionlog", subject_id="999999"
        )

        result = presenters.product_analytics(days=30, now=now)

        assert _feature(result, "push_enabled")["users"] == 0
        assert result["active_users"]["athletes"]["window"] == 0

    def test_an_ended_client_link_still_counts(self, now):
        athlete = UserFactory()
        _relationship(athlete=athlete, status=CoachAthlete.Status.ENDED)
        _event(
            EventName.SESSION_OPENED,
            actor=athlete,
            created=now - datetime.timedelta(days=1),
        )

        result = presenters.product_analytics(days=30, now=now)

        assert result["active_users"]["athletes"]["window"] == 1


class TestRequestClaimedByAnOlderInvite:
    def test_request_claimed_by_an_invite_sent_before_the_window_is_a_request(
        self, now
    ):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthleteFactory(
            coach=coach,
            athlete=athlete,
            invited_by=CoachAthlete.InvitedBy.ATHLETE,
            status=CoachAthlete.Status.ACTIVE,
        )
        CoachAthlete.objects.filter(pk=link.pk).update(
            created_at=now - datetime.timedelta(days=5),
            responded_at=now - datetime.timedelta(days=4),
        )
        invite = CoachInviteFactory(
            coach=coach,
            accepted_by=athlete,
            accepted_link=link,
            status=CoachInvite.Status.ACCEPTED,
        )
        CoachInvite.objects.filter(pk=invite.pk).update(
            created_at=now - datetime.timedelta(days=60),
            responded_at=now - datetime.timedelta(days=4),
        )

        paths = _paths(presenters.product_analytics(days=30, now=now))

        assert paths["email_invite"]["sent"] == 0
        assert paths["athlete_request"]["sent"] == 1
        assert paths["all"]["sent"] == 1


class TestProductAnalyticsView:
    def test_anonymous_is_redirected_to_login(self, client):
        resp = client.get(reverse("meso:product_analytics"))

        assert resp.status_code == 302
        assert "/accounts/login/" in resp["Location"]

    def test_authenticated_non_staff_is_forbidden(self, client):
        client.force_login(UserFactory())

        resp = client.get(reverse("meso:product_analytics"))

        assert resp.status_code == 403

    def test_staff_gets_200_with_the_four_section_headings(self, client):
        client.force_login(UserFactory(is_staff=True))

        resp = client.get(reverse("meso:product_analytics"))
        body = resp.content.decode()

        assert resp.status_code == 200
        headings = re.findall(r"<h2[^>]*>\s*([^<]+?)\s*</h2>", body)
        assert headings == [
            "Active users",
            "Activation funnel",
            "Feature adoption",
            "Email",
        ]

    def test_days_7_sets_the_context(self, client):
        client.force_login(UserFactory(is_staff=True))

        resp = client.get(reverse("meso:product_analytics"), {"days": 7})

        assert resp.context["days"] == 7

    def test_invalid_days_defaults_to_30_with_a_flashed_message(self, client):
        client.force_login(UserFactory(is_staff=True))

        resp = client.get(reverse("meso:product_analytics"), {"days": "abc"})

        assert resp.status_code == 200
        assert resp.context["days"] == 30
        messages = [str(m) for m in resp.context["messages"]]
        assert any("abc" in m for m in messages)

    def test_context_carries_active_analytics_and_the_email_dashboard_link(
        self, client
    ):
        client.force_login(UserFactory(is_staff=True))

        resp = client.get(reverse("meso:product_analytics"), {"days": 7})

        assert resp.context["active"] == "analytics"
        assert resp.context["email_dashboard_url"] == (
            reverse("notifications:email_dashboard") + "?days=7"
        )


class TestProductAnalyticsQueryCount:
    def _seed(self, now, n):
        for _ in range(n):
            coach = UserFactory()
            athlete = UserFactory()
            rel = _relationship(coach=coach, athlete=athlete)
            plan = _plan(rel)
            _touch_plan_created(plan, now - datetime.timedelta(days=1))
            week = _week_for(plan)
            _deliver_week(week, now - datetime.timedelta(days=1))
            _logged_set(athlete, plan, now - datetime.timedelta(days=1), week=week)
            _invite(coach, now - datetime.timedelta(days=1))
            _sent_email(
                EmailKind.COACH_INVITE, sent_at=now - datetime.timedelta(days=1)
            )
            _event(
                EventName.PLAN_CREATED,
                actor=coach,
                subject=plan,
                created=now - datetime.timedelta(days=1),
            )
            # Exercise every correlated subquery and every source, not just
            # the pending-invite path.
            invitee = UserFactory()
            accepted_rel = _accepted_invite(
                coach,
                invitee,
                now - datetime.timedelta(days=3),
                datetime.timedelta(hours=1),
            )
            accepted_plan = _plan(accepted_rel)
            accepted_week = _week_for(accepted_plan)
            _deliver_week(accepted_week, now - datetime.timedelta(days=2))
            _logged_set(
                invitee,
                accepted_plan,
                now - datetime.timedelta(days=1),
                week=accepted_week,
            )
            _plan_action(plan, now - datetime.timedelta(days=1))
            _agent_batch(plan, coach, now - datetime.timedelta(days=1))
            PushSubscription.objects.create(
                athlete=athlete,
                endpoint=f"https://push.example/{next(_seq)}",
                p256dh="k",
                auth="a",
            )
            for name in (EventName.SESSION_OPENED, EventName.SET_LOGGED):
                _event(name, actor=athlete, created=now - datetime.timedelta(days=1))
            sent = _sent_email(
                EmailKind.BLOCK_DELIVERED, sent_at=now - datetime.timedelta(days=1)
            )
            _email_event(sent, EmailEvent.EventType.OPEN)
            # An accepted athlete request through to a logged set, so the
            # athlete-request cohort scales with n too.
            requester = UserFactory()
            request = CoachAthleteFactory(
                coach=coach,
                athlete=requester,
                invited_by=CoachAthlete.InvitedBy.ATHLETE,
                status=CoachAthlete.Status.ACTIVE,
            )
            CoachAthlete.objects.filter(pk=request.pk).update(
                created_at=now - datetime.timedelta(days=3),
                responded_at=now - datetime.timedelta(days=3),
            )
            request_plan = _plan(request)
            request_week = _week_for(request_plan)
            _deliver_week(request_week, now - datetime.timedelta(days=2))
            _logged_set(
                requester,
                request_plan,
                now - datetime.timedelta(days=1),
                week=request_week,
            )

    def test_query_count_is_fixed_regardless_of_data_size(self, client, now):
        client.force_login(UserFactory(is_staff=True))
        url = reverse("meso:product_analytics")

        with CaptureQueriesContext(connection) as empty:
            client.get(url)

        self._seed(now, 1)
        with CaptureQueriesContext(connection) as small:
            client.get(url)

        self._seed(now, 8)
        with CaptureQueriesContext(connection) as large:
            client.get(url)

        assert len(empty.captured_queries) == len(small.captured_queries)
        assert len(small.captured_queries) == len(large.captured_queries)
