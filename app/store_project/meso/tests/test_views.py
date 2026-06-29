import json

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
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
    """The roster renders directly for any logged-in coach.

    Every other coach-side screen is now plan/session-bound and its bare URL
    redirects: designer/deliver (``TestBareDesignerDeliver``), review
    (``test_agent_endpoint``), and results (Phase 3 — see ``test_results``).
    """

    def test_roster_renders(self, client):
        # The roster is a coach surface: a non-coach is now routed to their
        # training home (N4 Phase 2), so log in an actual coach.
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, athlete=UserFactory())
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200


class TestBareDesignerDeliver:
    """The bare ``/designer/`` and ``/deliver/`` URLs resolve to a real plan.

    Phase 5 retired the client-side fixtures: with no ``plan_id`` the view
    redirects to the coach's working plan, or back to the roster if they have
    none. (The ``<plan_id>`` forms are covered in ``test_designer_save`` /
    ``test_deliver``.)
    """

    def _working_plan(self, coach):
        rel = CoachAthleteFactory(coach=coach, athlete=UserFactory())
        return PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)

    @pytest.mark.parametrize(
        "bare,target", [("designer", "designer_plan"), ("deliver", "deliver_plan")]
    )
    def test_redirects_to_working_plan(self, client, bare, target):
        coach = UserFactory()
        plan = self._working_plan(coach)
        client.force_login(coach)
        resp = client.get(reverse(f"meso:{bare}"))
        assert resp.status_code == 302
        assert resp.url == reverse(f"meso:{target}", kwargs={"plan_id": plan.pk})

    @pytest.mark.parametrize("bare", ["designer", "deliver"])
    def test_redirects_to_roster_without_a_plan(self, client, bare):
        client.force_login(UserFactory())
        resp = client.get(reverse(f"meso:{bare}"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")

    @pytest.mark.parametrize("bare", ["designer", "deliver"])
    def test_archived_plan_is_not_a_target(self, client, bare):
        coach = UserFactory()
        rel = CoachAthleteFactory(coach=coach, athlete=UserFactory())
        PlanFactory(relationship=rel, status=Plan.Status.ARCHIVED)
        client.force_login(coach)
        resp = client.get(reverse(f"meso:{bare}"))
        assert resp.url == reverse("meso:roster")  # archived → no working plan

    def _plan_with_prescription(self, coach):
        plan = self._working_plan(coach)
        presc = ExercisePrescriptionFactory(
            session=SessionFactory(
                week=WeekFactory(mesocycle=MesocycleFactory(plan=plan))
            )
        )
        return plan, presc

    def test_redirect_follows_the_last_edited_plan(self, client):
        # Grid autosaves write child rows; _touch_plan keeps Plan.modified in
        # sync so the bare redirect tracks the plan the coach last worked.
        coach = UserFactory()
        plan_a, presc_a = self._plan_with_prescription(coach)
        plan_b, presc_b = self._plan_with_prescription(coach)
        # Two athletes puts a free coach over the cap; comp so the autosaves
        # land (D6) and the test measures last-edited ordering, not the gate.
        CoachSubscription.comp(coach)
        client.force_login(coach)

        def patch(plan, presc):
            client.post(
                reverse(
                    "meso:api_prescription_patch",
                    kwargs={"plan_id": plan.pk, "pk": presc.pk},
                ),
                data=json.dumps({"reps": "8"}),
                content_type="application/json",
            )

        patch(plan_b, presc_b)
        resp = client.get(reverse("meso:designer"))
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": plan_b.pk})

        patch(plan_a, presc_a)  # now A is the most-recently-worked plan
        resp = client.get(reverse("meso:designer"))
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": plan_a.pk})
