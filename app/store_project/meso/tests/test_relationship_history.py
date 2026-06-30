"""Roster relationship-history view + re-invite (the "past athletes" surface).

When a coach (or athlete) ends a relationship, or a pending invite/request is
declined, the ``CoachAthlete`` row drops to a terminal status and vanishes from
the active roster — yet the row, the ``ended_at`` timestamp, and the (now
archived) plans all persist. This slice surfaces that history so a coach can see
who they used to train and **re-invite** them, reopening the existing link to a
fresh ``pending_coach_invite`` the athlete discovers on their training home.

Covers:

- ``CoachAthleteQuerySet.closed()`` (ended + declined; excludes active/pending);
- the ``CoachAthlete.is_closed`` / ``closed_at`` helpers;
- ``presenters.relationship_history`` (past + reconnecting split, coach scoping,
  demo exclusion, newest-first ordering, status labels);
- ``RelationshipHistoryView`` (login + coach-only gate, scoping, render);
- ``relationship_reinvite`` (reopen → ``pending_coach_invite``, closed-only
  no-op, coach-scoped 404, the seat gate, the athlete discovers it on home).
"""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.models import CoachAthlete
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def make_coach(email="coach@example.com", name="Coach Carter"):
    """A User who *is* a coach — a ``CoachProfile`` is the surface's gate."""
    coach = UserFactory(email=email, name=name)
    CoachProfileFactory(user=coach)
    return coach


def ended_link(coach, *, name="Past Athlete", ended_at=None):
    athlete = UserFactory(name=name)
    return CoachAthleteFactory(
        coach=coach,
        athlete=athlete,
        status=CoachAthlete.Status.ENDED,
        ended_at=ended_at or timezone.now(),
    )


# -- queryset / model ------------------------------------------------------


class TestClosedQuerySet:
    def test_closed_returns_ended_and_declined_only(self):
        coach = UserFactory()
        active = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        pending = CoachAthleteFactory(
            coach=coach, status=CoachAthlete.Status.PENDING_COACH_INVITE
        )
        declined = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.DECLINED)
        ended = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ENDED)

        closed = set(CoachAthlete.objects.for_coach(coach).closed())

        assert closed == {declined, ended}
        assert active not in closed
        assert pending not in closed

    def test_is_closed_property(self):
        ended = CoachAthleteFactory.build(status=CoachAthlete.Status.ENDED)
        declined = CoachAthleteFactory.build(status=CoachAthlete.Status.DECLINED)
        active = CoachAthleteFactory.build(status=CoachAthlete.Status.ACTIVE)
        pending = CoachAthleteFactory.build(
            status=CoachAthlete.Status.PENDING_COACH_INVITE
        )

        assert ended.is_closed
        assert declined.is_closed
        assert not active.is_closed
        assert not pending.is_closed

    def test_closed_at_is_ended_at_for_an_ended_link(self):
        link = CoachAthleteFactory(status=CoachAthlete.Status.ACTIVE)
        link.end()

        assert link.ended_at is not None
        assert link.closed_at == link.ended_at

    def test_closed_at_is_responded_at_for_a_declined_link(self):
        link = CoachAthleteFactory(status=CoachAthlete.Status.PENDING_ATHLETE_REQUEST)
        link.decline()

        assert link.responded_at is not None
        assert link.closed_at == link.responded_at

    def test_closed_at_is_none_for_an_open_link(self):
        active = CoachAthleteFactory.build(status=CoachAthlete.Status.ACTIVE)
        pending = CoachAthleteFactory.build(
            status=CoachAthlete.Status.PENDING_COACH_INVITE
        )

        assert active.closed_at is None
        assert pending.closed_at is None


# -- presenter -------------------------------------------------------------


class TestRelationshipHistoryPresenter:
    def test_splits_past_and_reconnecting(self):
        coach = make_coach()
        ended = ended_link(coach, name="Gone Gardner")
        declined = CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(name="Nope Nelson"),
            status=CoachAthlete.Status.DECLINED,
        )
        reinvited = CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(name="Back Baker"),
            status=CoachAthlete.Status.PENDING_COACH_INVITE,
        )
        # An active link never appears in history.
        CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(name="Active Avery"),
            status=CoachAthlete.Status.ACTIVE,
        )

        history = presenters.relationship_history(coach)

        past_ids = {row["id"] for row in history["past"]}
        reconnecting_ids = {row["id"] for row in history["reconnecting"]}
        assert past_ids == {ended.athlete_id, declined.athlete_id}
        assert reconnecting_ids == {reinvited.athlete_id}

    def test_scoped_to_the_coach(self):
        coach = make_coach()
        other = make_coach(email="other@example.com", name="Other One")
        ours = ended_link(coach, name="Ours")
        ended_link(other, name="Theirs")

        history = presenters.relationship_history(coach)

        names = {row["name"] for row in history["past"]}
        assert names == {ours.athlete.name}

    def test_excludes_demo_relationships(self):
        coach = make_coach()
        CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(name="Demo Dana"),
            status=CoachAthlete.Status.ENDED,
            ended_at=timezone.now(),
            is_demo=True,
        )
        kept = ended_link(coach, name="Real Riley")

        history = presenters.relationship_history(coach)

        assert {row["id"] for row in history["past"]} == {kept.athlete_id}

    def test_past_ordered_newest_first(self):
        coach = make_coach()
        older = ended_link(
            coach,
            name="Older",
            ended_at=timezone.now() - datetime.timedelta(days=30),
        )
        newer = ended_link(
            coach,
            name="Newer",
            ended_at=timezone.now() - datetime.timedelta(days=1),
        )

        history = presenters.relationship_history(coach)

        assert [row["id"] for row in history["past"]] == [
            newer.athlete_id,
            older.athlete_id,
        ]

    def test_status_labels(self):
        coach = make_coach()
        ended_link(coach, name="Ended Ed")
        CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(name="Declined Dee"),
            status=CoachAthlete.Status.DECLINED,
        )
        CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(name="Awaiting Amy"),
            status=CoachAthlete.Status.PENDING_COACH_INVITE,
        )

        history = presenters.relationship_history(coach)

        labels = {row["name"]: row["status_label"] for row in history["past"]}
        assert labels["Ended Ed"] == "Ended"
        assert labels["Declined Dee"] == "Declined"
        reconnecting = history["reconnecting"][0]
        assert reconnecting["status_label"] == "Awaiting response"
        # A re-invite is addressable for cancellation via its token.
        assert reconnecting["token"]


# -- history view ----------------------------------------------------------


class TestRelationshipHistoryView:
    def test_requires_login(self, client):
        resp = client.get(reverse("meso:relationship_history"))
        assert resp.status_code == 302
        assert "/accounts/login" in resp["Location"]

    def test_non_coach_redirected_to_training_home(self, client):
        athlete = UserFactory()  # no CoachProfile, no coach-side link
        client.force_login(athlete)

        resp = client.get(reverse("meso:relationship_history"))

        assert resp.status_code == 302
        assert resp["Location"] == reverse("meso:athlete_home")

    def test_coach_sees_their_past_athletes(self, client):
        coach = make_coach()
        link = ended_link(coach, name="Former Fiona")
        client.force_login(coach)

        resp = client.get(reverse("meso:relationship_history"))

        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Former Fiona" in body
        # A re-invite form addresses the link by token.
        assert (
            reverse("meso:relationship_reinvite", kwargs={"token": link.token}) in body
        )

    def test_does_not_leak_another_coachs_history(self, client):
        coach = make_coach()
        other = make_coach(email="other@example.com", name="Other Coach")
        ended_link(other, name="Secret Sam")
        client.force_login(coach)

        resp = client.get(reverse("meso:relationship_history"))

        assert "Secret Sam" not in resp.content.decode()

    def test_roster_links_to_history(self, client):
        coach = make_coach()
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        client.force_login(coach)

        resp = client.get(reverse("meso:roster"))

        assert reverse("meso:relationship_history") in resp.content.decode()


# -- re-invite -------------------------------------------------------------


class TestReinvite:
    def test_reopens_a_closed_link_to_pending_coach_invite(self, client):
        coach = make_coach()
        link = ended_link(coach, name="Comeback Kid")
        old_token = link.token
        client.force_login(coach)

        resp = client.post(
            reverse("meso:relationship_reinvite", kwargs={"token": link.token})
        )

        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.PENDING_COACH_INVITE
        assert link.invited_by == CoachAthlete.InvitedBy.COACH
        assert link.ended_at is None
        # Reopening rotates the token (the old history link dies).
        assert link.token != old_token

    def test_reinvited_athlete_sees_the_invite_on_their_home(self, client):
        coach = make_coach(name="Coach Quinn")
        link = ended_link(coach, name="Returning Rae")
        athlete = link.athlete
        client.force_login(coach)

        client.post(reverse("meso:relationship_reinvite", kwargs={"token": link.token}))

        pending = presenters.athlete_pending(athlete)
        coaches = {row["coach"] for row in pending["invites"]}
        assert "Coach Quinn" in coaches

    def test_requires_post(self, client):
        coach = make_coach()
        link = ended_link(coach)
        client.force_login(coach)

        resp = client.get(
            reverse("meso:relationship_reinvite", kwargs={"token": link.token})
        )

        assert resp.status_code == 405

    def test_coach_scoped_foreign_token_is_404(self, client):
        coach = make_coach()
        other = make_coach(email="other@example.com")
        link = ended_link(other, name="Not Yours")
        client.force_login(coach)

        resp = client.post(
            reverse("meso:relationship_reinvite", kwargs={"token": link.token})
        )

        assert resp.status_code == 404
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.ENDED

    def test_noop_on_an_active_link(self, client):
        coach = make_coach()
        link = CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(name="Still Here"),
            status=CoachAthlete.Status.ACTIVE,
        )
        client.force_login(coach)

        resp = client.post(
            reverse("meso:relationship_reinvite", kwargs={"token": link.token})
        )

        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.ACTIVE

    def test_seat_gate_blocks_a_free_coach_at_the_cap(self, client):
        # Free tier: FREE_SEAT_LIMIT == 1 active billable seat. One active
        # athlete already fills it, so re-activating a former athlete is gated.
        coach = make_coach()
        CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(name="Seat Taker"),
            status=CoachAthlete.Status.ACTIVE,
        )
        link = ended_link(coach, name="Wants Back")
        client.force_login(coach)

        resp = client.post(
            reverse("meso:relationship_reinvite", kwargs={"token": link.token}),
            follow=True,
        )

        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.ENDED  # not reopened
        messages = [m.message for m in resp.context["messages"]]
        assert any("free athlete limit" in m for m in messages)
