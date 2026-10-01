"""#649 — the athlete limit lands on the coach, never on the client.

A free coach has one seat. When a second invitee claims, or an athlete accepts a
coach's invite, and the coach has no seat, the acceptance is recorded as
``CoachAthlete.Status.ACCEPTED_WAITING``: not active (no seat, no program
access), not pending. The athlete only ever sees neutral wording; the coach gets
an email and a roster row with the upgrade CTA; and the coach's upgrade (any
``CoachSubscription`` save that leaves them active) flips the waiting links to
active, oldest first, up to the new capacity.
"""

import re
from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from store_project.meso.billing import access
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachInviteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory

from ._helpers import day

pytestmark = pytest.mark.django_db

WAITING = CoachAthlete.Status.ACCEPTED_WAITING
ACTIVE = CoachAthlete.Status.ACTIVE

WAITING_SUBJECT = "Casey Lee accepted your invite. Upgrade to start coaching them."
NEUTRAL = "You're connected to Alex Kim. They'll have your program ready soon."
# Words that must never reach the athlete (the limit is the coach's business).
FORBIDDEN = ("limit", "plan", "upgrade", "subscribe", "trial")


def make_coach(name="Alex Kim", email="alex@example.com"):
    coach = UserFactory(name=name, email=email)
    CoachProfileFactory(user=coach, display_name=name)
    return coach


def fill_seat(coach):
    """Take the coach's one free seat with an active athlete."""
    return CoachAthleteFactory(
        coach=coach, athlete=UserFactory(name="Riley Roe"), status=ACTIVE
    )


def waiting_link(coach, *, name="Casey Lee", responded_at=None):
    link = CoachAthleteFactory(
        coach=coach, athlete=UserFactory(name=name), status=WAITING
    )
    if responded_at is not None:
        CoachAthlete.objects.filter(pk=link.pk).update(responded_at=responded_at)
        link.refresh_from_db()
    return link


def flashed(resp):
    return [str(m) for m in resp.context["messages"]]


def visible_text(html):
    """Rendered text with tags, scripts and styles stripped (lower-cased)."""
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).lower()


# -- model -----------------------------------------------------------------


class TestModel:
    def test_accept_waiting_goes_to_accepted_waiting(self):
        link = CoachAthleteFactory(status=CoachAthlete.Status.PENDING_COACH_INVITE)
        link.accept(waiting=True)
        link.refresh_from_db()
        assert link.status == WAITING
        assert link.responded_at is not None
        assert link.is_waiting and not link.is_active

    def test_accept_default_still_activates(self):
        link = CoachAthleteFactory(status=CoachAthlete.Status.PENDING_COACH_INVITE)
        link.accept()
        assert link.status == ACTIVE

    def test_decline_works_from_waiting(self):
        link = CoachAthleteFactory(status=WAITING)
        link.decline()
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.DECLINED

    def test_open_leaves_a_waiting_link_alone(self):
        link = CoachAthleteFactory(status=WAITING)
        again = CoachAthlete.invite(coach=link.coach, athlete=link.athlete)
        assert again.pk == link.pk
        again.refresh_from_db()
        assert again.status == WAITING

    def test_waiting_queryset_and_not_billable(self):
        coach = make_coach()
        link = waiting_link(coach)
        assert list(CoachAthlete.objects.for_coach(coach).waiting()) == [link]
        assert access.active_seat_count(coach) == 0

    def test_invite_accept_waiting_passthrough(self):
        coach = make_coach()
        athlete = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email=athlete.email)
        link = invite.accept(athlete, waiting=True)
        invite.refresh_from_db()
        assert link.status == WAITING
        assert invite.status == CoachInvite.Status.ACCEPTED
        assert invite.accepted_link == link and invite.accepted_by == athlete


# -- invite time -----------------------------------------------------------


class TestCoachInviteWarning:
    url = None

    def setup_method(self):
        self.url = reverse("meso:coach_invite")

    def post(self, client, coach, capture, **data):
        client.force_login(coach)
        with capture(execute=True):
            return client.post(self.url, data, follow=True)

    def test_over_limit_still_creates_sends_and_warns(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        fill_seat(coach)
        resp = self.post(
            client,
            coach,
            django_capture_on_commit_callbacks,
            email="casey@example.com",
            name="Casey",
        )
        assert CoachInvite.objects.filter(coach=coach, email="casey@example.com")
        assert [m.to for m in mail.outbox] == [["casey@example.com"]]
        msgs = flashed(resp)
        # (The "Invite sent" flash is an on_commit callback: outside
        # ATOMIC_REQUESTS it fires after the response, so assert the mail.)
        assert (
            "Free covers 1 athlete. Casey won't be able to join until you start "
            "your trial or subscribe."
        ) in msgs

    def test_warning_names_the_email_without_a_label(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        fill_seat(coach)
        resp = self.post(
            client, coach, django_capture_on_commit_callbacks, email="c@example.com"
        )
        assert any(
            m.startswith("Free covers 1 athlete. c@example.com") for m in flashed(resp)
        )

    def test_outstanding_invites_count_toward_the_warning(
        self, client, django_capture_on_commit_callbacks
    ):
        # No active athlete yet: the first invite fits the one free seat, the
        # second is the one that cannot join.
        coach = make_coach()
        first = self.post(
            client, coach, django_capture_on_commit_callbacks, email="a@example.com"
        )
        assert not any("Free covers" in m for m in flashed(first))
        second = self.post(
            client, coach, django_capture_on_commit_callbacks, email="b@example.com"
        )
        assert any("Free covers 1 athlete. b@example.com" in m for m in flashed(second))

    def test_under_the_limit_no_warning(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        resp = self.post(
            client, coach, django_capture_on_commit_callbacks, email="a@example.com"
        )
        assert not any("Free covers" in m for m in flashed(resp))
        assert [m.to for m in mail.outbox] == [["a@example.com"]]

    def test_trialing_coach_never_warns(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        fill_seat(coach)
        CoachSubscription.start_trial_for(coach)
        resp = self.post(
            client, coach, django_capture_on_commit_callbacks, email="a@example.com"
        )
        assert CoachInvite.objects.filter(coach=coach).count() == 1
        assert not any("Free covers" in m for m in flashed(resp))

    def test_paid_coach_never_warns(self, client, django_capture_on_commit_callbacks):
        coach = make_coach()
        CoachSubscriptionFactory(coach=coach, status=CoachSubscription.Status.ACTIVE)
        for _ in range(3):
            fill_seat(coach)
        resp = self.post(
            client, coach, django_capture_on_commit_callbacks, email="a@example.com"
        )
        assert not any("Free covers" in m for m in flashed(resp))


# -- accept time: the athlete side ----------------------------------------


class TestClaimOverLimit:
    def setup_method(self):
        self.coach = make_coach()
        fill_seat(self.coach)
        self.athlete = UserFactory(name="Casey Lee", email="casey@example.com")
        self.invite, _ = CoachInvite.open_for(
            coach=self.coach, email="casey@example.com", label="Casey"
        )
        self.url = reverse("meso:invite_claim", kwargs={"token": self.invite.token})

    def claim(self, client, capture):
        client.force_login(self.athlete)
        with capture(execute=True):
            return client.post(self.url, {"action": "accept"}, follow=True)

    def test_claim_materializes_a_waiting_link(
        self, client, django_capture_on_commit_callbacks
    ):
        before = access.active_seat_count(self.coach)
        resp = self.claim(client, django_capture_on_commit_callbacks)
        link = CoachAthlete.objects.get(coach=self.coach, athlete=self.athlete)
        assert link.status == WAITING
        self.invite.refresh_from_db()
        assert self.invite.status == CoachInvite.Status.ACCEPTED
        assert self.invite.accepted_link == link
        assert access.active_seat_count(self.coach) == before
        assert resp.redirect_chain[-1][0] == reverse("meso:athlete_home")

    def test_athlete_sees_only_neutral_wording(
        self, client, django_capture_on_commit_callbacks
    ):
        resp = self.claim(client, django_capture_on_commit_callbacks)
        msgs = flashed(resp)
        assert NEUTRAL in msgs
        for text in [m.lower() for m in msgs]:
            for word in FORBIDDEN:
                assert word not in text, word
        # The page chrome has its own words ("see the plan"): the waiting state
        # must add none, so compare against a bare athlete's home.
        bare = UserFactory()
        client.force_login(bare)
        baseline = visible_text(
            client.get(reverse("meso:athlete_home")).content.decode()
        )
        page = visible_text(resp.content.decode())
        for word in FORBIDDEN:
            assert page.count(word) == baseline.count(word), word
        # The holding card is on the page.
        assert "they'll have your program ready soon" in visible_text(
            resp.content.decode()
        )

    def test_coach_gets_exactly_one_email_athlete_none(
        self, client, django_capture_on_commit_callbacks
    ):
        self.claim(client, django_capture_on_commit_callbacks)
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == [self.coach.email]
        assert msg.subject == WAITING_SUBJECT
        assert reverse("meso:roster") in msg.body
        assert not any(self.athlete.email in m.to for m in mail.outbox)

    def test_claim_with_a_seat_free_still_activates(
        self, client, django_capture_on_commit_callbacks
    ):
        CoachAthlete.objects.filter(coach=self.coach).delete()
        resp = self.claim(client, django_capture_on_commit_callbacks)
        link = CoachAthlete.objects.get(coach=self.coach, athlete=self.athlete)
        assert link.status == ACTIVE
        assert mail.outbox == []
        assert "You're now training with Alex Kim." in flashed(resp)

    def test_no_email_for_a_sandbox_coach(
        self, client, django_capture_on_commit_callbacks
    ):
        with mock.patch(
            "store_project.meso.views.meso_sandbox.is_sandbox",
            side_effect=lambda u: u.pk == self.coach.pk,
        ):
            self.claim(client, django_capture_on_commit_callbacks)
        assert mail.outbox == []

    def test_mail_failure_does_not_break_the_claim(
        self, client, django_capture_on_commit_callbacks
    ):
        with mock.patch(
            "store_project.meso.views.send_athlete_waiting_email",
            side_effect=RuntimeError("smtp down"),
        ):
            self.claim(client, django_capture_on_commit_callbacks)
        assert (
            CoachAthlete.objects.get(coach=self.coach, athlete=self.athlete).status
            == WAITING
        )

    def test_roster_no_longer_shows_pending_for_the_claimed_invite(self, client):
        client.force_login(self.athlete)
        client.post(self.url, {"action": "accept"})
        client.force_login(self.coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "awaiting reply" not in body
        assert "Accepted — waiting on your plan" in body


class TestInviteAcceptOverLimit:
    def test_athlete_accepting_a_coach_invite_is_waiting_and_neutral(self, client):
        coach = make_coach()
        fill_seat(coach)
        athlete = UserFactory(name="Casey Lee")
        link = CoachAthleteFactory(
            coach=coach,
            athlete=athlete,
            status=CoachAthlete.Status.PENDING_COACH_INVITE,
        )
        client.force_login(athlete)
        resp = client.post(
            reverse("meso:invite_accept", kwargs={"token": link.token}), follow=True
        )
        link.refresh_from_db()
        assert link.status == WAITING
        msgs = flashed(resp)
        assert NEUTRAL in msgs
        for m in msgs:
            for word in FORBIDDEN:
                assert word not in m.lower()
        assert not any("take on new athletes" in m for m in msgs)

    def test_coach_accepting_a_request_over_limit_is_unchanged(self, client):
        from store_project.meso.views import SEAT_LIMIT_MESSAGE

        coach = make_coach()
        fill_seat(coach)
        link = CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(),
            status=CoachAthlete.Status.PENDING_ATHLETE_REQUEST,
        )
        client.force_login(coach)
        resp = client.post(
            reverse("meso:invite_accept", kwargs={"token": link.token}), follow=True
        )
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST
        assert SEAT_LIMIT_MESSAGE in flashed(resp)

    def test_athlete_request_coach_is_a_friendly_noop_on_a_waiting_link(self, client):
        coach = make_coach()
        athlete = UserFactory()
        link = CoachAthleteFactory(coach=coach, athlete=athlete, status=WAITING)
        client.force_login(athlete)
        resp = client.post(
            reverse("meso:athlete_request_coach"), {"email": coach.email}, follow=True
        )
        link.refresh_from_db()
        assert link.status == WAITING
        assert any("already" in m for m in flashed(resp))


# -- roster ----------------------------------------------------------------


class TestRoster:
    def get(self, client, coach):
        client.force_login(coach)
        return client.get(reverse("meso:roster")).content.decode()

    def test_waiting_row_with_cta(self, client):
        coach = make_coach()
        fill_seat(coach)
        link = waiting_link(coach)
        body = self.get(client, coach)
        assert "Accepted — waiting on your plan" in body
        assert "Casey Lee" in body
        assert reverse("meso:billing_start_trial") in body
        assert reverse("meso:invite_decline", kwargs={"token": link.token}) in body

    def test_trial_used_offers_subscribe_only(self, client):
        coach = make_coach()
        fill_seat(coach)
        waiting_link(coach)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.FREE,
            trial_end=timezone.now() - timedelta(days=30),
        )
        body = self.get(client, coach)
        assert "Accepted — waiting on your plan" in body
        assert reverse("meso:billing_start_trial") not in body
        assert reverse("meso:billing_subscribe") in body

    def test_waiting_only_coach_is_not_first_run(self, client):
        coach = make_coach()
        waiting_link(coach)
        body = self.get(client, coach)
        assert "Welcome to your coaching workspace" not in body

    def test_waiting_rows_are_not_counted_as_athletes(self, client):
        coach = make_coach()
        waiting_link(coach)
        body = self.get(client, coach)
        assert "0 athletes" in body

    def test_over_capacity_notice_with_cta_for_outstanding_invites(self, client):
        coach = make_coach()
        fill_seat(coach)
        CoachInviteFactory(coach=coach, email="b@example.com")
        body = self.get(client, coach)
        assert "Free covers 1 athlete" in body
        assert reverse("meso:billing_start_trial") in body

    def test_no_notice_within_capacity(self, client):
        coach = make_coach()
        CoachInviteFactory(coach=coach, email="b@example.com")
        assert "Free covers 1 athlete" not in self.get(client, coach)


class TestCoachRemovesWaiting:
    def url(self, link):
        return reverse("meso:invite_decline", kwargs={"token": link.token})

    def test_coach_declines_a_waiting_link(self, client):
        coach = make_coach()
        link = waiting_link(coach)
        client.force_login(coach)
        resp = client.post(self.url(link))
        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.DECLINED

    def test_stranger_cannot(self, client):
        coach = make_coach()
        link = waiting_link(coach)
        client.force_login(UserFactory())
        assert client.post(self.url(link)).status_code == 403
        link.refresh_from_db()
        assert link.status == WAITING

    def test_the_athlete_cannot_use_the_coach_remove(self, client):
        coach = make_coach()
        link = waiting_link(coach)
        client.force_login(link.athlete)
        assert client.post(self.url(link)).status_code == 403


# -- no access while waiting ------------------------------------------------


class TestWaitingGrantsNoAccess:
    def seed(self):
        coach = make_coach()
        link = waiting_link(coach)
        plan = PlanFactory(
            relationship=link, title="Secret Waiting Plan", status=Plan.Status.ACTIVE
        )
        meso = MesocycleFactory(plan=plan)
        week = WeekFactory(mesocycle=meso, index=1)
        session = day(week, day_number=1, name="Waiting Lower")
        return coach, link, plan, session

    def test_athlete_home_shows_no_program(self, client):
        _, link, _, _ = self.seed()
        client.force_login(link.athlete)
        body = client.get(reverse("meso:athlete_home")).content.decode()
        assert "Secret Waiting Plan" not in body
        assert "Waiting Lower" not in body

    def test_athlete_cannot_open_or_log_the_session(self, client):
        _, link, _, session = self.seed()
        client.force_login(link.athlete)
        assert (
            client.get(
                reverse("meso:athlete_session", kwargs={"pk": session.pk})
            ).status_code
            == 404
        )
        resp = client.post(
            reverse("meso:athlete_log_session", kwargs={"pk": session.pk}),
            data="{}",
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_coach_designer_404(self, client):
        coach, _, plan, _ = self.seed()
        client.force_login(coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 404


# -- activation on upgrade --------------------------------------------------


class TestActivateWaiting:
    def statuses(self, *links):
        return [CoachAthlete.objects.get(pk=link.pk).status for link in links]

    def test_start_trial_activates_all_oldest_first(
        self, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        fill_seat(coach)
        now = timezone.now()
        a = waiting_link(coach, name="A", responded_at=now - timedelta(hours=3))
        b = waiting_link(coach, name="B", responded_at=now - timedelta(hours=2))
        with django_capture_on_commit_callbacks(execute=True):
            CoachSubscription.start_trial_for(coach)
        assert self.statuses(a, b) == [ACTIVE, ACTIVE]

    def test_capacity_limits_and_orders_by_responded_at(
        self, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        now = timezone.now()
        # Created (pk) order is the REVERSE of responded_at order.
        late = waiting_link(coach, name="Late", responded_at=now - timedelta(hours=1))
        mid = waiting_link(coach, name="Mid", responded_at=now - timedelta(hours=2))
        early = waiting_link(coach, name="Early", responded_at=now - timedelta(hours=3))
        with mock.patch.object(access, "effective_seat_limit", return_value=2):
            with django_capture_on_commit_callbacks(execute=True):
                CoachSubscription.start_trial_for(coach)
        assert self.statuses(early, mid, late) == [ACTIVE, ACTIVE, WAITING]

    def test_existing_active_seats_reduce_capacity(
        self, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        fill_seat(coach)
        first = waiting_link(coach, name="First")
        second = waiting_link(coach, name="Second")
        with mock.patch.object(access, "effective_seat_limit", return_value=2):
            with django_capture_on_commit_callbacks(execute=True):
                CoachSubscription.start_trial_for(coach)
        assert self.statuses(first, second).count(ACTIVE) == 1

    def test_stripe_webhook_style_upsert_activates(
        self, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        link = waiting_link(coach)
        with django_capture_on_commit_callbacks(execute=True):
            CoachSubscription.objects.update_or_create(
                coach=coach,
                defaults={
                    "status": CoachSubscription.Status.ACTIVE,
                    "stripe_subscription_id": "sub_123",
                },
            )
        assert self.statuses(link) == [ACTIVE]

    def test_comp_activates(self, django_capture_on_commit_callbacks):
        coach = make_coach()
        link = waiting_link(coach)
        with django_capture_on_commit_callbacks(execute=True):
            CoachSubscription.comp(coach)
        assert self.statuses(link) == [ACTIVE]

    def test_save_that_leaves_the_coach_free_does_not(
        self, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        link = waiting_link(coach)
        with django_capture_on_commit_callbacks(execute=True):
            CoachSubscription.objects.update_or_create(
                coach=coach, defaults={"status": CoachSubscription.Status.CANCELED}
            )
            CoachSubscriptionFactory(
                coach=make_coach("Other", "o@example.com"),
                status=CoachSubscription.Status.FREE,
            )
        assert self.statuses(link) == [WAITING]

    def test_a_failure_in_activation_never_propagates(
        self, django_capture_on_commit_callbacks
    ):
        coach = make_coach()
        link = waiting_link(coach)
        with mock.patch(
            "store_project.meso.billing.activation.activate_waiting",
            side_effect=RuntimeError("boom"),
        ):
            with django_capture_on_commit_callbacks(execute=True):
                sub = CoachSubscription.start_trial_for(coach)
        assert sub.status == CoachSubscription.Status.TRIALING
        assert self.statuses(link) == [WAITING]

    def test_activate_waiting_returns_the_activated_links(self):
        from store_project.meso.billing.activation import activate_waiting

        coach = make_coach()
        CoachSubscriptionFactory(coach=coach, status=CoachSubscription.Status.COMPED)
        link = waiting_link(coach)
        assert [x.pk for x in activate_waiting(coach.pk)] == [link.pk]
        assert activate_waiting(coach.pk) == []
