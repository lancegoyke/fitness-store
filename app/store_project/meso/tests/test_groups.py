"""Groups slice (S1) Phase 1 — the group + membership spine and read surface.

A coach groups several of their athletes who train together; later phases give
the group a shared program and per-athlete auto-adjusts. Phase 1 is the
tenancy-correct foundation: ``MesoGroup`` (coach-owned) + ``GroupMembership``
(group ↔ an *active* ``CoachAthlete`` link), scoped reads, and the roster
*Groups* card + a coach-scoped group detail page.

These tests pin the scoping contract (see ``docs/meso/groups-plan.md``): a coach
sees only their own groups; a group's *displayed* members are scoped to active
links, so ending a relationship hides the member without deleting the membership
row (reopening the link restores them).
"""

import pytest
from django.urls import reverse

from store_project.meso import presenters
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.factories import GroupMembershipFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import GroupMembership
from store_project.meso.models import InvalidTransition
from store_project.meso.models import MesoGroup
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def make_member(group, *, name="Member One"):
    """An athlete with an active link to the group's coach, added to the group."""
    athlete = UserFactory(name=name)
    CoachAthleteFactory(
        coach=group.coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    return group.add_athlete(athlete)


# -- model + scoping --------------------------------------------------------


class TestMesoGroupScoping:
    def test_for_coach_isolates_per_coach(self):
        coach = UserFactory()
        other = UserFactory()
        mine = MesoGroupFactory(coach=coach)
        MesoGroupFactory(coach=other)
        assert list(MesoGroup.objects.for_coach(coach)) == [mine]

    def test_active_excludes_archived(self):
        coach = UserFactory()
        live = MesoGroupFactory(coach=coach, status=MesoGroup.Status.ACTIVE)
        MesoGroupFactory(coach=coach, status=MesoGroup.Status.ARCHIVED)
        assert list(MesoGroup.objects.for_coach(coach).active()) == [live]


# -- membership helpers -----------------------------------------------------


class TestAddRemoveMember:
    def test_add_athlete_creates_membership_off_active_link(self):
        group = MesoGroupFactory()
        athlete = UserFactory()
        link = CoachAthleteFactory(
            coach=group.coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        membership = group.add_athlete(athlete)
        assert membership.group == group
        assert membership.relationship == link

    def test_add_athlete_is_idempotent(self):
        group = MesoGroupFactory()
        athlete = UserFactory()
        CoachAthleteFactory(
            coach=group.coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        group.add_athlete(athlete)
        group.add_athlete(athlete)
        assert group.memberships.count() == 1

    def test_add_athlete_without_active_link_raises(self):
        group = MesoGroupFactory()
        stranger = UserFactory()
        with pytest.raises(InvalidTransition):
            group.add_athlete(stranger)

    def test_add_athlete_with_only_pending_link_raises(self):
        group = MesoGroupFactory()
        athlete = UserFactory()
        CoachAthleteFactory(
            coach=group.coach,
            athlete=athlete,
            status=CoachAthlete.Status.PENDING_COACH_INVITE,
        )
        with pytest.raises(InvalidTransition):
            group.add_athlete(athlete)

    def test_add_athlete_linked_to_another_coach_raises(self):
        group = MesoGroupFactory()
        other_coach = UserFactory()
        athlete = UserFactory()
        CoachAthleteFactory(
            coach=other_coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        with pytest.raises(InvalidTransition):
            group.add_athlete(athlete)

    def test_remove_athlete_drops_membership(self):
        group = MesoGroupFactory()
        athlete = UserFactory()
        CoachAthleteFactory(
            coach=group.coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        group.add_athlete(athlete)
        group.remove_athlete(athlete)
        assert group.memberships.count() == 0

    def test_remove_athlete_is_idempotent(self):
        group = MesoGroupFactory()
        athlete = UserFactory()
        # Never added — removing is a no-op, not an error.
        group.remove_athlete(athlete)
        assert group.memberships.count() == 0

    def test_membership_unique_per_group_relationship(self):
        group = MesoGroupFactory()
        athlete = UserFactory()
        link = CoachAthleteFactory(
            coach=group.coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        GroupMembershipFactory(group=group, relationship=link)
        with pytest.raises(Exception):
            GroupMembership.objects.create(group=group, relationship=link)


class TestActiveMemberUsers:
    def test_lists_active_members(self):
        group = MesoGroupFactory()
        make_member(group, name="Aaron Adams")
        make_member(group, name="Beth Brown")
        names = [u.name for u in group.active_member_users()]
        assert names == ["Aaron Adams", "Beth Brown"]

    def test_excludes_ended_link_without_deleting_membership(self):
        group = MesoGroupFactory()
        membership = make_member(group, name="Casey Cole")
        membership.relationship.end()
        assert group.active_member_users() == []
        # The membership row survives the ended link.
        assert group.memberships.count() == 1

    def test_restores_member_when_link_reopened(self):
        group = MesoGroupFactory()
        membership = make_member(group, name="Dana Diaz")
        link = membership.relationship
        link.end()
        assert group.active_member_users() == []
        # Reopen the same link (re-invite + accept) → the member is back.
        CoachAthlete.invite(coach=group.coach, athlete=link.athlete)
        link.refresh_from_db()
        link.accept()
        assert [u.name for u in group.active_member_users()] == ["Dana Diaz"]


# -- presenters -------------------------------------------------------------


class TestPresenters:
    def test_roster_group_shape(self):
        group = MesoGroupFactory(name="Tue/Thu Squad", focus="Hypertrophy")
        make_member(group, name="Aaron Adams")
        make_member(group, name="Beth Brown")
        data = presenters.roster_group(group)
        assert data["name"] == "Tue/Thu Squad"
        assert data["focus"] == "Hypertrophy"
        assert [m["initials"] for m in data["member_objs"]] == ["AA", "BB"]
        assert "2 participants" in data["meta"]
        assert "Hypertrophy" in data["meta"]
        assert data["status_label"]

    def test_group_detail_folds_flags_across_members(self):
        group = MesoGroupFactory(name="Squad")
        m1 = make_member(group, name="Aaron Adams")
        m2 = make_member(group, name="Beth Brown")
        ContraindicationFactory(
            athlete=m1.relationship.athlete, text="L knee — avoid deep flexion"
        )
        ContraindicationFactory(
            athlete=m2.relationship.athlete, text="L knee — avoid deep flexion"
        )
        ContraindicationFactory(
            athlete=m2.relationship.athlete, text="R shoulder — neutral grip"
        )
        data = presenters.group_detail(group)
        assert data["name"] == "Squad"
        assert [m["name"] for m in data["members"]] == ["Aaron Adams", "Beth Brown"]
        # Flags folded to a unique, sorted set across the group.
        assert data["flags"] == ["L knee", "R shoulder"]


# -- views ------------------------------------------------------------------


class TestRosterGroups:
    def test_roster_renders_coachs_groups(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        group = MesoGroupFactory(coach=coach, name="Morning Crew")
        make_member(group, name="Aaron Adams")
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Morning Crew" in body

    def test_roster_hides_other_coachs_group(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        other = UserFactory()
        MesoGroupFactory(coach=other, name="Not Yours")
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Not Yours" not in body

    def test_roster_hides_archived_group(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        MesoGroupFactory(
            coach=coach, name="Old Squad", status=MesoGroup.Status.ARCHIVED
        )
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Old Squad" not in body


class TestGroupDetailView:
    def test_detail_lists_members_and_flags(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach, name="Squad")
        member = make_member(group, name="Aaron Adams")
        ContraindicationFactory(
            athlete=member.relationship.athlete, text="L knee — avoid deep flexion"
        )
        client.force_login(coach)
        resp = client.get(reverse("meso:group", kwargs={"pk": group.pk}))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Aaron Adams" in body
        assert "L knee" in body

    def test_detail_foreign_group_is_404(self, client):
        coach = UserFactory()
        other = UserFactory()
        group = MesoGroupFactory(coach=other)
        client.force_login(coach)
        resp = client.get(reverse("meso:group", kwargs={"pk": group.pk}))
        assert resp.status_code == 404

    def test_detail_unknown_group_is_404(self, client):
        client.force_login(UserFactory())
        resp = client.get(reverse("meso:group", kwargs={"pk": 999999}))
        assert resp.status_code == 404

    def test_detail_requires_login(self, client):
        group = MesoGroupFactory()
        resp = client.get(reverse("meso:group", kwargs={"pk": group.pk}))
        assert resp.status_code == 302
