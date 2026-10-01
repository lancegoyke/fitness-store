"""A stale tab must not overwrite newer values; failures keep typed input (#657)."""

import re

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachProfile
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

SETTINGS = reverse("meso:settings")


@pytest.fixture
def link():
    return CoachAthleteFactory(
        coach=UserFactory(), athlete=UserFactory(name="Jordan Intake")
    )


def record_url(link):
    return reverse("meso:athlete_record", kwargs={"pk": link.athlete_id})


def texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def hidden_initial(body, field):
    tag = re.search(rf'<input[^>]*name="initial_{field}"[^>]*>', body)
    assert tag, f"no hidden initial_{field}"
    return re.search(r'value="([^"]*)"', tag.group(0)).group(1)


class TestIntakeStaleTab:
    def test_two_tabs_keep_both_edits(self, client, link):
        AthleteProfile.objects.create(user=link.athlete, goals="old goal")
        client.force_login(link.coach)
        # Tab B saves a new goal first.
        client.post(
            record_url(link), {"goals": "tab B goal", "initial_goals": "old goal"}
        )
        # Tab A, still holding the old goal, saves only a new note.
        client.post(
            record_url(link),
            {
                "goals": "old goal",
                "initial_goals": "old goal",
                "notes": "tab A note",
                "initial_notes": "",
            },
        )
        profile = AthleteProfile.objects.get(user=link.athlete)
        assert profile.goals == "tab B goal"
        assert profile.notes == "tab A note"

    def test_same_field_conflict_refuses_it_and_keeps_the_rest(self, client, link):
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
        )
        assert response.status_code == 200
        profile = AthleteProfile.objects.get(user=link.athlete)
        assert profile.goals == "tab B goal"
        assert profile.notes == "tab A note"
        assert (
            "Goals were changed elsewhere since you opened this page"
            " — review and save again"
        ) in texts(response)
        body = response.content.decode()
        assert re.search(r"<textarea[^>]*name=\"goals\"[^>]*>tab A goal<", body)
        assert hidden_initial(body, "goals") == "tab B goal"

    def test_validation_failure_rerenders_with_typed_values(self, client, link):
        client.force_login(link.coach)
        response = client.post(
            record_url(link),
            {
                "goals": "typed goals",
                "notes": "typed notes",
                "label": "x" * 256,
            },
        )
        assert response.status_code == 200
        body = response.content.decode()
        assert "typed goals" in body
        assert "typed notes" in body
        assert not AthleteProfile.objects.filter(
            user=link.athlete, goals="typed goals"
        ).exists()

    def test_invalid_date_rerenders_with_typed_values(self, client, link):
        client.force_login(link.coach)
        response = client.post(
            record_url(link),
            {"goals": "typed goals", "training_started": "2999-01-01"},
        )
        assert response.status_code == 200
        assert "typed goals" in response.content.decode()

    def test_page_renders_hidden_initials(self, client, link):
        AthleteProfile.objects.create(user=link.athlete, goals="g", notes="n")
        client.force_login(link.coach)
        body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete_id})
        ).content.decode()
        assert hidden_initial(body, "goals") == "g"
        assert hidden_initial(body, "notes") == "n"


@pytest.fixture
def coach(client):
    user = UserFactory(name="Old Name")
    CoachProfile.objects.create(
        user=user, display_name="Old Display", avoid_rules="old rules"
    )
    client.force_login(user)
    return user


class TestSettingsStaleTab:
    def test_omitted_coach_fields_are_untouched(self, client, coach):
        response = client.post(SETTINGS, {"name": "New Name"})
        assert response.status_code == 302
        coach.refresh_from_db()
        assert coach.name == "New Name"
        profile = CoachProfile.objects.get(user=coach)
        assert profile.display_name == "Old Display"
        assert profile.avoid_rules == "old rules"

    def test_stale_tab_does_not_revert_other_fields(self, client, coach):
        CoachProfile.objects.filter(user=coach).update(display_name="Tab B display")
        response = client.post(
            SETTINGS,
            {
                "name": "Tab A Name",
                "initial_name": "Old Name",
                "display_name": "Old Display",
                "initial_display_name": "Old Display",
                "programming_style": "",
                "avoid_rules": "old rules",
                "unit": "lb",
            },
        )
        assert response.status_code == 302
        coach.refresh_from_db()
        assert coach.name == "Tab A Name"
        assert CoachProfile.objects.get(user=coach).display_name == "Tab B display"

    def test_same_field_conflict_refuses_it_and_keeps_the_rest(self, client, coach):
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
        assert response.status_code == 200
        coach.refresh_from_db()
        assert coach.name == "Tab A Name"
        assert CoachProfile.objects.get(user=coach).display_name == "Tab B display"
        assert (
            "Display name was changed elsewhere since you opened this page"
            " — review and save again"
        ) in texts(response)
        body = response.content.decode()
        assert 'value="Tab A display"' in body
        assert hidden_initial(body, "display_name") == "Tab B display"
