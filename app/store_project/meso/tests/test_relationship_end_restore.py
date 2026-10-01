"""Ending a coaching relationship is deliberate, reversible and told (#651).

Covers the in-page confirm step (and the server refusing a bare end POST), the
``ended_by`` / ``ended_archived_plans`` record ``end()`` keeps, the coach-only
30-day **Restore** (no re-invite), the "your coach ended this" email, and the
roster no longer falling back to the first-run checklist for a coach who has
only past athletes.
"""

import datetime
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from store_project.meso import views
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

ACTIVE = CoachAthlete.Status.ACTIVE
ENDED = CoachAthlete.Status.ENDED


def make_coach(name="Coach Carter"):
    coach = UserFactory(name=name)
    CoachProfileFactory(user=coach)
    return coach


def make_link(coach=None, athlete=None, **kwargs):
    coach = coach or make_coach()
    athlete = athlete or UserFactory(name="Jordan Ellis")
    return CoachAthleteFactory(coach=coach, athlete=athlete, status=ACTIVE, **kwargs)


def end_url(link):
    return reverse("meso:relationship_end", kwargs={"token": link.token})


def restore_url(link):
    return reverse("meso:relationship_restore", kwargs={"token": link.token})


def status_of(plan):
    plan.refresh_from_db()
    return plan.status


def three_plans(link):
    """A draft + an active plan, plus one that was archived before any end."""
    draft = PlanFactory(relationship=link, title="Draft One", status=Plan.Status.DRAFT)
    live = PlanFactory(
        relationship=link, title="Strength Foundations", status=Plan.Status.ACTIVE
    )
    old = PlanFactory(relationship=link, title="Old Block", status=Plan.Status.ARCHIVED)
    return draft, live, old


def coach_ends(client, link):
    client.force_login(link.coach)
    return client.post(end_url(link), {"confirm": "1"})


# -- confirm step ----------------------------------------------------------


class TestConfirmStep:
    def test_profile_has_confirm_panel_naming_athlete_and_program(self, client):
        link = make_link()
        PlanFactory(
            relationship=link, title="Strength Foundations", status=Plan.Status.ACTIVE
        )
        client.force_login(link.coach)

        body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete_id})
        ).content.decode()

        assert "End coaching with Jordan Ellis?" in body
        assert "Strength Foundations" in body
        assert "will disappear from their app" in body
        assert 'name="confirm"' in body
        assert "return confirm(" not in body

    def test_confirm_panel_without_plans_says_past_athletes(self, client):
        link = make_link()
        client.force_login(link.coach)

        body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete_id})
        ).content.decode()

        assert "move to Past athletes." in body

    def test_bare_post_does_not_end(self, client):
        link = make_link()
        draft, live, _ = three_plans(link)
        client.force_login(link.coach)

        resp = client.post(end_url(link))

        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.status == ACTIVE
        assert status_of(draft) == Plan.Status.DRAFT
        assert status_of(live) == Plan.Status.ACTIVE

    def test_confirmed_post_ends(self, client):
        link = make_link()
        coach_ends(client, link)
        link.refresh_from_db()
        assert link.status == ENDED


# -- end() records who and what --------------------------------------------


class TestEndRecords:
    def test_coach_end_snapshots_exactly_the_plans_it_archived(self, client):
        link = make_link()
        draft, live, old = three_plans(link)

        coach_ends(client, link)

        link.refresh_from_db()
        assert link.ended_by == "coach"
        assert link.ended_archived_plans == {
            str(draft.pk): "draft",
            str(live.pk): "active",
        }
        assert status_of(old) == Plan.Status.ARCHIVED

    def test_athlete_end_records_athlete(self, client):
        link = make_link()
        client.force_login(link.athlete)
        client.post(end_url(link), {"confirm": "1"})
        link.refresh_from_db()
        assert link.status == ENDED
        assert link.ended_by == "athlete"

    def test_reinvite_clears_the_record(self, client):
        link = make_link()
        three_plans(link)
        coach_ends(client, link)
        CoachAthlete.objects.filter(pk=link.pk).update(
            ended_by="coach", ended_archived_plans={"1": "draft"}
        )

        client.post(reverse("meso:relationship_reinvite", kwargs={"token": link.token}))

        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.PENDING_COACH_INVITE
        assert link.ended_by == ""
        assert link.ended_archived_plans == {}

    def test_add_self_reopen_clears_the_record(self):
        coach = make_coach()
        link = CoachAthlete.add_self(coach)
        CoachAthlete.objects.filter(pk=link.pk).update(
            status=ENDED, ended_by="coach", ended_archived_plans={"1": "draft"}
        )

        link = CoachAthlete.add_self(coach)

        link.refresh_from_db()
        assert link.status == ACTIVE
        assert link.ended_by == ""
        assert link.ended_archived_plans == {}


# -- restore ---------------------------------------------------------------


class TestRestore:
    def test_restore_puts_everything_back(self, client):
        link = make_link()
        draft, live, old = three_plans(link)
        coach_ends(client, link)
        client.force_login(link.athlete)
        assert (
            "Strength Foundations"
            not in client.get(reverse("meso:athlete_home")).content.decode()
        )
        client.force_login(link.coach)

        resp = client.post(restore_url(link), follow=True)

        assert resp.redirect_chain[-1][0] == reverse("meso:roster")
        link.refresh_from_db()
        assert link.status == ACTIVE
        assert link.ended_at is None
        assert link.ended_by == ""
        assert link.ended_archived_plans == {}
        assert status_of(draft) == Plan.Status.DRAFT
        assert status_of(live) == Plan.Status.ACTIVE
        assert status_of(old) == Plan.Status.ARCHIVED
        messages = [m.message for m in resp.context["messages"]]
        assert any(
            "Restored Jordan Ellis. Their program is back in their app." in m
            for m in messages
        )
        client.force_login(link.athlete)
        assert (
            "Strength Foundations"
            in client.get(reverse("meso:athlete_home")).content.decode()
        )

    def test_restore_sends_no_email(self, client, django_capture_on_commit_callbacks):
        link = make_link()
        coach_ends(client, link)
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            client.post(restore_url(link))
        assert mail.outbox == []

    def test_refused_after_30_days(self, client):
        link = make_link()
        draft, live, _ = three_plans(link)
        coach_ends(client, link)
        CoachAthlete.objects.filter(pk=link.pk).update(
            ended_at=timezone.now() - datetime.timedelta(days=31)
        )

        client.post(restore_url(link))

        link.refresh_from_db()
        assert link.status == ENDED
        assert status_of(live) == Plan.Status.ARCHIVED

    def test_refused_when_athlete_ended(self, client):
        link = make_link()
        _, live, _ = three_plans(link)
        client.force_login(link.athlete)
        client.post(end_url(link), {"confirm": "1"})
        client.force_login(link.coach)

        client.post(restore_url(link))

        link.refresh_from_db()
        assert link.status == ENDED
        assert status_of(live) == Plan.Status.ARCHIVED

    def test_foreign_coach_gets_404(self, client):
        link = make_link()
        coach_ends(client, link)
        client.force_login(make_coach("Other Coach"))
        assert client.post(restore_url(link)).status_code == 404
        link.refresh_from_db()
        assert link.status == ENDED

    def test_seat_cap_refuses_restore(self, client):
        coach = make_coach()
        link = make_link(coach=coach)
        _, live, _ = three_plans(link)
        coach_ends(client, link)
        CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=ACTIVE)
        assert not CoachSubscription.FREE_SEAT_LIMIT > 1

        resp = client.post(restore_url(link), follow=True)

        link.refresh_from_db()
        assert link.status == ENDED
        assert status_of(live) == Plan.Status.ARCHIVED
        messages = [m.message for m in resp.context["messages"]]
        assert views.SEAT_LIMIT_MESSAGE in messages

    def test_get_is_not_allowed(self, client):
        link = make_link()
        coach_ends(client, link)
        assert client.get(restore_url(link)).status_code == 405
        link.refresh_from_db()
        assert link.status == ENDED

    def test_past_athletes_shows_restore_only_for_eligible_rows(self, client):
        coach = make_coach()
        eligible = make_link(coach=coach, athlete=UserFactory(name="Eli Gible"))
        stale = make_link(coach=coach, athlete=UserFactory(name="Stale Sam"))
        by_athlete = make_link(coach=coach, athlete=UserFactory(name="Quit Quinn"))
        client.force_login(coach)
        for link in (eligible, stale):
            client.post(end_url(link), {"confirm": "1"})
        CoachAthlete.objects.filter(pk=stale.pk).update(
            ended_at=timezone.now() - datetime.timedelta(days=45)
        )
        client.force_login(by_athlete.athlete)
        client.post(end_url(by_athlete), {"confirm": "1"})
        client.force_login(coach)

        body = client.get(reverse("meso:relationship_history")).content.decode()

        assert restore_url(eligible) in body
        assert restore_url(stale) not in body
        assert restore_url(by_athlete) not in body
        assert body.count("Restore</button>") == 1


# -- email -----------------------------------------------------------------


class TestEndedEmail:
    def test_coach_end_emails_the_athlete_once(
        self, client, django_capture_on_commit_callbacks
    ):
        link = make_link(coach=make_coach("Coach Carter"))
        client.force_login(link.coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(end_url(link), {"confirm": "1"})

        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == [link.athlete.email]
        assert msg.subject == "Coach Carter has ended your coaching on Meso"
        assert (
            "Coach Carter has ended your coaching on Meso. "
            "Your training history stays in your account."
        ) in msg.body
        assert reverse("meso:athlete_home") in msg.body
        assert "relationship_ended" in msg.extra_headers.get(
            "X-SES-MESSAGE-TAGS", ""
        ).replace("-", "_")

    def test_bare_post_sends_nothing(self, client, django_capture_on_commit_callbacks):
        link = make_link()
        client.force_login(link.coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(end_url(link))
        assert mail.outbox == []

    def test_athlete_end_sends_nothing(
        self, client, django_capture_on_commit_callbacks
    ):
        link = make_link()
        client.force_login(link.athlete)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(end_url(link), {"confirm": "1"})
        assert mail.outbox == []

    def test_opted_out_athlete_gets_nothing(
        self, client, django_capture_on_commit_callbacks
    ):
        link = make_link()
        AthleteProfile.objects.create(user=link.athlete, delivery_email_opt_out=True)
        client.force_login(link.coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(end_url(link), {"confirm": "1"})
        assert mail.outbox == []

    def test_self_and_demo_links_send_nothing(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        self_link = CoachAthleteFactory(
            coach=coach, athlete=coach, is_self=True, status=ACTIVE
        )
        demo = CoachAthleteFactory(coach=coach, is_demo=True, status=ACTIVE)
        client.force_login(coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(end_url(self_link), {"confirm": "1"})
            client.post(end_url(demo), {"confirm": "1"})
        assert mail.outbox == []
        self_link.refresh_from_db()
        assert self_link.status == ENDED

    def test_email_failure_does_not_break_the_end(
        self, client, django_capture_on_commit_callbacks
    ):
        link = make_link()
        client.force_login(link.coach)
        with mock.patch.object(
            views, "send_relationship_ended_email", side_effect=RuntimeError("smtp")
        ):
            with django_capture_on_commit_callbacks(execute=True):
                resp = client.post(end_url(link), {"confirm": "1"})
        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.status == ENDED


# -- roster ----------------------------------------------------------------


class TestRosterAfterEnding:
    def test_only_past_athletes_is_not_first_run(self, client):
        link = make_link()
        coach_ends(client, link)

        body = client.get(reverse("meso:roster")).content.decode()

        assert "Three steps" not in body
        assert "Welcome to your coaching workspace" not in body
        assert reverse("meso:relationship_history") in body
        assert "No active athletes right now." in body

    def test_brand_new_coach_still_sees_the_checklist(self, client):
        coach = make_coach()
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Three steps" in body
