"""UAT-2 polish: #654 (roster), #667 (self-coach home), #669 (prompt exclusivity)."""

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso import views
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachInviteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import ExerciseSlotFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionSlotFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.tests.test_athlete_onboarding import seed
from store_project.meso.tests.test_onboarding_uat2 import _claim
from store_project.meso.tests.test_onboarding_uat2 import _invite
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

ROSTER = reverse("meso:roster")
HOME = reverse("meso:athlete_home")
SIGNUP = reverse("account_signup")
ADD_SELF = "+ Add yourself as an athlete"
WELCOME = "Welcome to your coaching workspace"
Status = CoachAthlete.Status


def get(client, user, url):
    client.force_login(user)
    return client.get(url).content.decode()


def coach_user():
    coach = UserFactory()
    CoachProfileFactory(user=coach)
    return coach


def plan_with_exercise(link, *, exercise=True, delivered=False, is_template=False):
    plan = PlanFactory(relationship=link, is_template=is_template)
    meso = MesocycleFactory(plan=plan)
    WeekFactory(mesocycle=meso, delivered_at=timezone.now() if delivered else None)
    if exercise:
        ExerciseSlotFactory(session_slot=SessionSlotFactory(mesocycle=meso))
    return plan


class TestAddYourselfOnce:
    def test_pending_invite_shows_one_add_yourself(self, client):
        coach = coach_user()
        CoachInviteFactory(coach=coach)
        assert get(client, coach, ROSTER).count(ADD_SELF) == 1

    def test_with_new_program_panel_still_one(self, client):
        coach = coach_user()
        CoachInviteFactory(coach=coach)
        CoachAthleteFactory(coach=coach)
        body = get(client, coach, ROSTER)
        assert "+ New program" in body
        assert body.count(ADD_SELF) == 1


class TestGettingStartedChecklist:
    def done(self, client, coach):
        body = get(client, coach, ROSTER)
        return body, body.count("data-step-done")

    def test_fresh_coach_sees_card_with_nothing_ticked(self, client):
        body, done = self.done(client, coach_user())
        assert "Get started" in body
        assert WELCOME in body
        assert done == 0

    def test_sent_invite_ticks_step_one_only(self, client):
        coach = coach_user()
        CoachInviteFactory(coach=coach)
        assert self.done(client, coach)[1] == 1

    def test_self_link_ticks_step_one(self, client):
        coach = coach_user()
        CoachAthlete.add_self(coach)
        assert self.done(client, coach)[1] == 1

    def test_template_ticks_step_two(self, client):
        coach = coach_user()
        PlanFactory(relationship=None, is_template=True, owner=coach)
        assert views._getting_started_steps(coach)["written"]

    def test_plan_without_exercises_does_not_tick_step_two(self):
        coach = coach_user()
        plan_with_exercise(CoachAthleteFactory(coach=coach), exercise=False)
        assert not views._getting_started_steps(coach)["written"]

    def test_plan_with_exercise_ticks_step_two(self):
        coach = coach_user()
        plan_with_exercise(CoachAthleteFactory(coach=coach))
        assert views._getting_started_steps(coach)["written"]

    def test_demo_plan_does_not_tick_step_two(self):
        coach = coach_user()
        plan_with_exercise(CoachAthleteFactory(coach=coach, is_demo=True))
        steps = views._getting_started_steps(coach)
        assert not steps["written"]
        assert not steps["invited"]

    def test_delivered_week_ticks_step_three(self):
        coach = coach_user()
        plan_with_exercise(CoachAthleteFactory(coach=coach), delivered=True)
        assert views._getting_started_steps(coach)["delivered"]

    def test_all_done_hides_the_card(self, client):
        coach = coach_user()
        plan_with_exercise(CoachAthleteFactory(coach=coach), delivered=True)
        PlanFactory(relationship=None, is_template=True, owner=coach)
        assert WELCOME not in get(client, coach, ROSTER)

    def test_ended_history_alone_hides_the_card(self, client):
        coach = coach_user()
        CoachAthleteFactory(coach=coach, status=Status.ENDED)
        assert WELCOME not in get(client, coach, ROSTER)


class TestSignupSubtitle:
    def test_plain_signup_has_no_email_instruction(self, client):
        body = client.get(SIGNUP).content.decode()
        assert "Enter your email below" not in body

    def test_plain_signup_subtitle_does_not_repeat_the_heading(self, client):
        # #688.5: the heading says "Create your account"; the subtitle offers
        # the sign-in door instead of echoing it.
        body = client.get(SIGNUP).content.decode()
        assert "Already have an account?" in body
        assert "Create your account." not in body
        assert body.count("Create your account") == 1

    def test_claim_signup_keeps_coach_copy(self, client):
        invite = _invite()
        client.get(_claim(invite.token))
        body = client.get(SIGNUP, {"next": _claim(invite.token)}).content.decode()
        assert "Join Sam Rivera on Meso" in body
        assert "Create your account to start training with Sam Rivera." in body


class TestSelfCoachedHome:
    def self_coach(self):
        coach = coach_user()
        CoachAthlete.add_self(coach)
        return coach

    def test_self_coach_sees_no_coach_cta_and_self_label(self, client):
        body = get(client, self.self_coach(), HOME)
        assert "Are you a coach?" not in body
        start = body.index('data-testid="your-coaches"')
        assert (
            "You (self-coached)"
            in body[start : body.index("<!-- /your-coaches -->", start)]
        )

    def test_has_coach_false_for_self_only(self):
        assert presenters.athlete_pending(self.self_coach())["has_coach"] is False

    def test_plain_athlete_still_sees_coach_cta(self, client):
        assert "Are you a coach?" in get(client, UserFactory(), HOME)

    def test_coached_athlete_unchanged(self, client):
        athlete, *_ = seed()
        body = get(client, athlete, HOME)
        assert "Are you a coach?" not in body
        assert "You (self-coached)" not in body
        assert presenters.athlete_pending(athlete)["has_coach"] is True


def test_first_log_tip_is_server_rendered_suppressed(client):
    athlete, *_ = seed()
    body = get(client, athlete, HOME)
    tag = body[body.index('data-coachmark-key="firstlog-home"') :].split(">", 1)[0]
    assert "data-prompt-suppressed" in tag


def test_pwa_cache_version():
    assert views.PWA_CACHE_VERSION == "meso-pwa-v10"
