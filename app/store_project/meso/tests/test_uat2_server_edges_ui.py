"""UAT-2 edges: roster checklist (#683) and intake/settings polish (#680)."""

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from store_project.meso import views
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import Plan
from store_project.meso.tests.test_polish_654_667_669 import WELCOME
from store_project.meso.tests.test_polish_654_667_669 import coach_user
from store_project.meso.tests.test_polish_654_667_669 import plan_with_exercise
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

ROSTER = reverse("meso:roster")
HOME = reverse("meso:athlete_home")
SETTINGS = reverse("meso:settings")
Status = CoachAthlete.Status


def texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class TestChecklistEverHappened:
    @pytest.mark.parametrize(
        "status", [Status.PENDING_ATHLETE_REQUEST, Status.DECLINED]
    )
    def test_athlete_request_does_not_tick_invite(self, status):
        coach = coach_user()
        CoachAthleteFactory(
            coach=coach, status=status, invited_by=CoachAthlete.InvitedBy.ATHLETE
        )
        assert not views._getting_started_steps(coach)["invited"]

    def test_accepted_athlete_request_still_ticks_invite(self):
        coach = coach_user()
        CoachAthleteFactory(
            coach=coach,
            status=Status.ACTIVE,
            invited_by=CoachAthlete.InvitedBy.ATHLETE,
        )
        assert views._getting_started_steps(coach)["invited"]

    def test_archiving_every_plan_keeps_steps_and_hides_card(self, client):
        coach = coach_user()
        plan_with_exercise(CoachAthleteFactory(coach=coach), delivered=True)
        PlanFactory(relationship=None, is_template=True, owner=coach)
        Plan.objects.update(status=Plan.Status.ARCHIVED)
        steps = views._getting_started_steps(coach)
        assert steps["written"]
        assert steps["delivered"]
        client.force_login(coach)
        assert WELCOME not in client.get(ROSTER).content.decode()


class TestSelfCoachedProgramLine:
    def test_self_coached_program_reads_self_coached(self, client):
        user = coach_user()
        plan_with_exercise(CoachAthlete.add_self(user))
        client.force_login(user)
        body = client.get(HOME).content.decode()
        assert 'data-testid="athlete-program"' in body
        assert "Self-coached" in body
        assert f"Coach {user.display_name()}" not in body

    def test_normal_coach_still_reads_coach_name(self, client):
        link = CoachAthleteFactory(coach=UserFactory(name="Sam Rivera"))
        plan_with_exercise(link)
        client.force_login(link.athlete)
        assert "Coach Sam Rivera" in client.get(HOME).content.decode()


class TestWaitingLeaveRow:
    def test_leave_confirm_is_a_compact_single_row(self, client):
        athlete = UserFactory()
        CoachAthleteFactory(
            coach=UserFactory(name="Wanda Waits"),
            athlete=athlete,
            status=Status.ACCEPTED_WAITING,
        )
        client.force_login(athlete)
        body = client.get(HOME).content.decode()
        assert 'class="meso-leave"' in body
        assert "Leave?" in body


@pytest.fixture
def link():
    return CoachAthleteFactory(
        coach=UserFactory(), athlete=UserFactory(name="Jordan Intake")
    )


def record_url(link):
    return reverse("meso:athlete_record", kwargs={"pk": link.athlete_id})


def profile_url(link):
    return reverse("meso:athlete", kwargs={"pk": link.athlete_id})


class TestIntakeErrorsAndPRG:
    def test_all_errors_surface_at_once(self, client, link):
        client.force_login(link.coach)
        response = client.post(
            record_url(link),
            {"label": "x" * 300, "training_started": "2999-01-01"},
            follow=True,
        )
        shown = texts(response)
        assert "Name must be 255 characters or fewer." in shown
        assert "Training started cannot be in the future." in shown

    def test_overlong_label_rejected_server_side(self, client, link):
        client.force_login(link.coach)
        client.post(record_url(link), {"label": "x" * 256})
        link.refresh_from_db()
        assert link.label == ""

    def test_label_endpoint_rejects_overlong_label(self, client, link):
        client.force_login(link.coach)
        response = client.post(
            reverse("meso:athlete_label", kwargs={"pk": link.athlete_id}),
            {"label": "x" * 256},
        )
        assert response.status_code == 400
        link.refresh_from_db()
        assert link.label == ""

    def test_validation_error_redirects_then_is_consumed(self, client, link):
        client.force_login(link.coach)
        response = client.post(
            record_url(link), {"goals": "typed goals", "training_started": "2999-01-01"}
        )
        assert response.status_code == 302
        assert response.url == profile_url(link)
        first = client.get(response.url).content.decode()
        assert "typed goals" in first
        assert "Training started cannot be in the future." in first
        second = client.get(profile_url(link)).content.decode()
        assert "typed goals" not in second
        assert "cannot be in the future" not in second

    def test_conflict_redirects_shows_values_and_message_once(self, client, link):
        AthleteProfile.objects.create(user=link.athlete, goals="tab B goal")
        client.force_login(link.coach)
        response = client.post(
            record_url(link), {"goals": "tab A goal", "initial_goals": "old goal"}
        )
        assert response.status_code == 302
        assert response.url == profile_url(link)
        first = client.get(response.url)
        assert "tab A goal" in first.content.decode()
        assert (
            "Goals were changed elsewhere since you opened this page"
            " — review and save again"
        ) in texts(first)
        second = client.get(profile_url(link))
        assert "tab A goal" not in second.content.decode()
        assert not texts(second)

    def test_partial_save_notice(self, client, link):
        AthleteProfile.objects.create(user=link.athlete, goals="tab B goal")
        client.force_login(link.coach)
        response = client.post(
            record_url(link),
            {
                "goals": "tab A goal",
                "initial_goals": "old goal",
                "notes": "tab A note",
                "initial_notes": "",
            },
            follow=True,
        )
        assert "Saved notes. Goals changed elsewhere — review and save again." in (
            response.content.decode()
        )


class TestSettingsPRG:
    @pytest.fixture
    def coach(self, client):
        user = UserFactory(name="Old Name")
        CoachProfile.objects.create(user=user, display_name="Old Display")
        client.force_login(user)
        return user

    def test_conflict_redirects_and_consumes(self, client, coach):
        CoachProfile.objects.filter(user=coach).update(display_name="Tab B display")
        response = client.post(
            SETTINGS,
            {
                "name": "Tab A Name",
                "initial_name": "Old Name",
                "display_name": "Tab A display",
                "initial_display_name": "Old Display",
            },
        )
        assert response.status_code == 302
        assert response.url == SETTINGS
        first = client.get(SETTINGS)
        assert 'value="Tab A display"' in first.content.decode()
        assert (
            "Saved your name. Display name changed elsewhere — review and save again."
            in (first.content.decode())
        )
        second = client.get(SETTINGS)
        assert "Tab A display" not in second.content.decode()

    def test_validation_error_redirects_with_errors(self, client, coach):
        response = client.post(
            SETTINGS, {"name": "Typed New Name", "display_name": "", "unit": "bogus"}
        )
        assert response.status_code == 302
        first = client.get(SETTINGS).content.decode()
        assert "Typed New Name" in first
        assert "errorlist" in first
        assert "Typed New Name" not in client.get(SETTINGS).content.decode()
