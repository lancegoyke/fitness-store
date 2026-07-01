"""Groups slice (S1) Phase 2b — create a brand-new group from the roster.

Phases 1–2a stood up the group + membership spine and a group's shared program,
but a group could still only be born from the seed/admin. Phase 2b is the
create-group entry point: a coach names a group, gives it a focus, and picks
members from their roster.

These tests pin the contract (see ``docs/archive/meso/groups-plan.md``): ``name`` is
required, members are scoped to the coach's *active* links (a foreign/stale pick
is silently ignored, never a leak or a 500), and a successful create lands on
the new group's detail page. The model helper (``create_for_coach``) carries the
tenancy guard; the view is a thin form POST over it.
"""

import uuid

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import MesoGroup
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def active_athlete(coach, *, name="Member One"):
    """An athlete with an active link to ``coach`` (eligible for membership)."""
    athlete = UserFactory(name=name)
    CoachAthleteFactory(coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE)
    return athlete


# -- model helper -----------------------------------------------------------


class TestCreateForCoach:
    def test_creates_group_owned_by_coach(self):
        coach = UserFactory()
        group = MesoGroup.create_for_coach(
            coach, name="Tue/Thu Squad", focus="Hypertrophy"
        )
        assert group.pk is not None
        assert group.coach == coach
        assert group.name == "Tue/Thu Squad"
        assert group.focus == "Hypertrophy"
        assert group.status == MesoGroup.Status.ACTIVE

    def test_adds_given_athletes_off_active_links(self):
        coach = UserFactory()
        a = active_athlete(coach, name="Aaron Adams")
        b = active_athlete(coach, name="Beth Brown")
        group = MesoGroup.create_for_coach(coach, name="Squad", athletes=[a, b])
        names = [u.name for u in group.active_member_users()]
        assert names == ["Aaron Adams", "Beth Brown"]

    def test_focus_defaults_blank_and_athletes_optional(self):
        coach = UserFactory()
        group = MesoGroup.create_for_coach(coach, name="Solo")
        assert group.focus == ""
        assert group.active_member_users() == []

    def test_skips_athlete_without_active_link(self):
        # A stranger (no active link) is skipped, not raised — the helper stays
        # safe even if a caller passes an ineligible athlete.
        coach = UserFactory()
        good = active_athlete(coach, name="Good One")
        stranger = UserFactory(name="No Link")
        group = MesoGroup.create_for_coach(
            coach, name="Squad", athletes=[good, stranger]
        )
        names = [u.name for u in group.active_member_users()]
        assert names == ["Good One"]


# -- view: create from the roster -------------------------------------------


class TestGroupCreateView:
    def test_post_creates_group_with_members_and_redirects_to_detail(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        a = active_athlete(coach, name="Aaron Adams")
        b = active_athlete(coach, name="Beth Brown")
        client.force_login(coach)
        resp = client.post(
            reverse("meso:group_create"),
            data={
                "name": "Tue/Thu Squad",
                "focus": "Strength",
                "athletes": [str(a.pk), str(b.pk)],
            },
        )
        group = MesoGroup.objects.for_coach(coach).get()
        assert resp.status_code == 302
        assert resp.url == reverse("meso:group", kwargs={"pk": group.pk})
        assert group.name == "Tue/Thu Squad"
        assert group.focus == "Strength"
        assert {u.name for u in group.active_member_users()} == {
            "Aaron Adams",
            "Beth Brown",
        }

    def test_post_ignores_foreign_athlete(self, client):
        # Picking another coach's athlete (a tampered/stale form) never adds them.
        coach = UserFactory()
        mine = active_athlete(coach, name="Mine")
        other_coach = UserFactory()
        foreign = active_athlete(other_coach, name="Theirs")
        client.force_login(coach)
        client.post(
            reverse("meso:group_create"),
            data={"name": "Squad", "athletes": [str(mine.pk), str(foreign.pk)]},
        )
        group = MesoGroup.objects.for_coach(coach).get()
        assert [u.name for u in group.active_member_users()] == ["Mine"]

    def test_post_ignores_pending_link_athlete(self, client):
        coach = UserFactory()
        pending = UserFactory(name="Pending")
        CoachAthleteFactory(
            coach=coach,
            athlete=pending,
            status=CoachAthlete.Status.PENDING_COACH_INVITE,
        )
        client.force_login(coach)
        client.post(
            reverse("meso:group_create"),
            data={"name": "Squad", "athletes": [str(pending.pk)]},
        )
        group = MesoGroup.objects.for_coach(coach).get()
        assert group.active_member_users() == []

    def test_post_tolerates_malformed_athlete_id(self, client):
        # A non-UUID value must never reach the ORM as a query error (500).
        coach = UserFactory()
        client.force_login(coach)
        resp = client.post(
            reverse("meso:group_create"),
            data={"name": "Squad", "athletes": ["not-a-uuid", str(uuid.uuid4())]},
        )
        assert resp.status_code == 302
        group = MesoGroup.objects.for_coach(coach).get()
        assert group.active_member_users() == []

    def test_blank_name_creates_nothing(self, client):
        coach = UserFactory()
        client.force_login(coach)
        resp = client.post(
            reverse("meso:group_create"), data={"name": "   ", "focus": "x"}
        )
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert MesoGroup.objects.for_coach(coach).count() == 0

    def test_get_not_allowed(self, client):
        client.force_login(UserFactory())
        resp = client.get(reverse("meso:group_create"))
        assert resp.status_code == 405

    def test_requires_login(self, client):
        resp = client.post(reverse("meso:group_create"), data={"name": "Squad"})
        assert resp.status_code == 302
        assert MesoGroup.objects.count() == 0


# -- roster: the create-group affordance ------------------------------------


class TestRosterCreateForm:
    def test_roster_offers_create_form_with_pickable_athletes(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        athlete = active_athlete(coach, name="Aaron Adams")
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        # The form posts to the create endpoint and offers the coach's athlete.
        assert reverse("meso:group_create") in body
        assert 'name="athletes"' in body
        assert str(athlete.pk) in body
        assert "Aaron Adams" in body

    def test_roster_create_form_excludes_foreign_athlete(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        other_coach = UserFactory()
        active_athlete(other_coach, name="Foreign Athlete")
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Foreign Athlete" not in body
