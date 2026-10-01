"""Athlete home layout (#640, #641): the program first, one prompt, coaches listed."""

import datetime
import re

import pytest
from django.urls import reverse

from store_project.meso import views
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.tests.test_athlete_onboarding import seed
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

HOME = reverse("meso:athlete_home")
Status = CoachAthlete.Status


def home(client, athlete):
    client.force_login(athlete)
    return client.get(HOME).content.decode()


def coaches_panel(body):
    start = body.index('data-testid="your-coaches"')
    return body[start : body.index("<!-- /your-coaches -->", start)]


class TestYourCoachesListsTheCoach:
    """#640: the panel names the coach the banner and program card name."""

    def test_active_coach_is_listed_with_since_date(self, client):
        coach = UserFactory(name="Sam Rivera")
        athlete, *_ = seed(coach=coach)
        link = CoachAthlete.objects.get(athlete=athlete)
        CoachAthlete.objects.filter(pk=link.pk).update(
            responded_at=datetime.datetime(2026, 10, 1, 12, tzinfo=datetime.UTC)
        )
        panel = coaches_panel(home(client, athlete))
        assert "Sam Rivera" in panel
        assert "since Oct 1" in panel

    def test_waiting_coach_listed_without_plan_or_limit_wording(self, client):
        coach = UserFactory(name="Wanda Waits")
        athlete = UserFactory()
        CoachAthleteFactory(
            coach=coach, athlete=athlete, status=Status.ACCEPTED_WAITING
        )
        panel = coaches_panel(home(client, athlete)).lower()
        assert "wanda waits" in panel
        for word in ("limit", "upgrade", "plan", "seat", "billing"):
            assert word not in panel

    def test_request_form_collapsed_with_a_coach(self, client):
        athlete, *_ = seed()
        panel = coaches_panel(home(client, athlete))
        assert not re.search(r"<details[^>]*\sopen", panel)
        assert "+ Request a coach" in panel

    def test_request_form_collapsed_with_a_waiting_coach(self, client):
        athlete = UserFactory()
        CoachAthleteFactory(athlete=athlete, status=Status.ACCEPTED_WAITING)
        panel = coaches_panel(home(client, athlete))
        assert not re.search(r"<details[^>]*\sopen", panel)

    def test_request_form_open_with_no_coach(self, client):
        panel = coaches_panel(home(client, UserFactory()))
        assert re.search(r"<details[^>]*\sopen", panel)


class TestProgramFirst:
    """#641: session/program lead; prompts, coaches panel, cross-sell follow."""

    def test_program_precedes_coaches_panel_and_every_prompt(self, client):
        athlete, *_ = seed()
        body = home(client, athlete)
        program = body.index('data-testid="athlete-program"')
        assert program < body.index('data-testid="your-coaches"')
        prompts = [m.start() for m in re.finditer(r"data-prompt-priority=", body)]
        assert prompts, "prompt cards carry data-prompt-priority"
        assert all(program < p for p in prompts)

    def test_first_session_row_precedes_prompts(self, client):
        athlete, _c, session, _p = seed()
        body = home(client, athlete)
        row = body.index(reverse("meso:athlete_session", kwargs={"pk": session.pk}))
        assert row < body.index("data-prompt-priority=")

    def test_prompt_priorities_are_push_install_tip_in_order(self, client):
        athlete, *_ = seed()
        body = home(client, athlete)
        priorities = re.findall(r'data-prompt-priority="(\d+)"', body)
        assert priorities == ["1", "2", "3"]
        assert body.index('id="meso-push-cta"') < body.index('id="meso-install-card"')
        assert body.index('id="meso-install-card"') < body.index(
            'data-coachmark-key="firstlog-home"'
        )

    def test_every_prompt_is_dismissible_and_keyed(self, client):
        athlete, *_ = seed()
        body = home(client, athlete)
        assert len(re.findall(r"data-prompt-dismiss-key=", body)) == 3

    def test_pending_invite_cards_sit_in_the_coaches_panel_below_program(self, client):
        athlete, *_ = seed()
        CoachAthleteFactory(athlete=athlete, status=Status.PENDING_COACH_INVITE)
        body = home(client, athlete)
        assert body.index("Invited you to train") > body.index(
            'data-testid="athlete-program"'
        )


class TestCoachCrossSell:
    def test_absent_for_a_coached_athlete(self, client):
        athlete, *_ = seed()
        assert "Are you a coach?" not in home(client, athlete)

    def test_absent_for_a_waiting_athlete(self, client):
        athlete = UserFactory()
        CoachAthleteFactory(athlete=athlete, status=Status.ACCEPTED_WAITING)
        assert "Are you a coach?" not in home(client, athlete)

    def test_present_for_an_athlete_with_no_coach(self, client):
        assert "Are you a coach?" in home(client, UserFactory())


class TestPwaCacheVersion:
    def test_bumped_and_served(self, client):
        assert views.PWA_CACHE_VERSION != "meso-pwa-v8"
        body = client.get(reverse("meso:service_worker")).content.decode()
        assert views.PWA_CACHE_VERSION in body
