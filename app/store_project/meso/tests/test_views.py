import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.models import CoachAthlete
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


class TestRosterScoping:
    def test_roster_shows_only_active_athletes(self, client):
        coach = UserFactory()
        mine = UserFactory(name="Maya Okonkwo")
        pending_athlete = UserFactory(name="Devon Reyes")
        CoachAthleteFactory(coach=coach, athlete=mine)
        CoachAthlete.invite(coach=coach, athlete=pending_athlete)  # not active
        CoachAthleteFactory(coach=UserFactory(), athlete=UserFactory(name="Priya Nair"))

        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Maya Okonkwo" in body
        assert "Devon Reyes" not in body  # pending, not on roster
        assert "Priya Nair" not in body  # another coach's athlete

    def test_roster_requires_login(self, client):
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url


class TestProfileScoping:
    def test_profile_renders_real_athlete(self, client):
        coach = UserFactory()
        athlete = UserFactory(name="Maya Okonkwo")
        CoachAthleteFactory(coach=coach, athlete=athlete)
        ContraindicationFactory(
            athlete=athlete, text="L knee — avoid deep knee flexion under load"
        )
        client.force_login(coach)
        resp = client.get(reverse("meso:athlete", kwargs={"pk": athlete.pk}))
        body = resp.content.decode()
        assert resp.status_code == 200
        assert "Maya Okonkwo" in body
        assert "avoid deep knee flexion" in body  # real contraindication

    def test_profile_404_for_unrelated_athlete(self, client):
        coach = UserFactory()
        stranger = UserFactory()
        client.force_login(coach)
        resp = client.get(reverse("meso:athlete", kwargs={"pk": stranger.pk}))
        assert resp.status_code == 404

    def test_profile_404_for_pending_link(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        CoachAthlete.invite(coach=coach, athlete=athlete)  # pending, not active
        client.force_login(coach)
        resp = client.get(reverse("meso:athlete", kwargs={"pk": athlete.pk}))
        assert resp.status_code == 404


class TestInviteActions:
    def test_athlete_accepts_invite(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        client.force_login(athlete)
        resp = client.post(reverse("meso:invite_accept", kwargs={"token": link.token}))
        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.is_active

    def test_wrong_party_cannot_accept(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        client.force_login(coach)  # coach is not the recipient of their own invite
        resp = client.post(reverse("meso:invite_accept", kwargs={"token": link.token}))
        assert resp.status_code == 403
        link.refresh_from_db()
        assert link.is_pending

    def test_recipient_declines(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        client.force_login(athlete)
        client.post(reverse("meso:invite_decline", kwargs={"token": link.token}))
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.DECLINED

    def test_either_party_ends(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        link.accept()
        client.force_login(coach)
        client.post(reverse("meso:relationship_end", kwargs={"token": link.token}))
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.ENDED

    def test_accept_requires_login(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        resp = client.post(reverse("meso:invite_accept", kwargs={"token": link.token}))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        client.force_login(athlete)
        resp = client.get(reverse("meso:invite_accept", kwargs={"token": link.token}))
        assert resp.status_code == 405


class TestScreensRender:
    """Every Meso screen renders for a logged-in coach (guards template reverses)."""

    @pytest.mark.parametrize(
        "name", ["roster", "designer", "review", "deliver", "results"]
    )
    def test_renders(self, client, name):
        client.force_login(UserFactory())
        resp = client.get(reverse(f"meso:{name}"))
        assert resp.status_code == 200
