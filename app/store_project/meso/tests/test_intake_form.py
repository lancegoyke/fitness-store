"""One form, one Save: the athlete intake and Meso settings (#646)."""

import re
from pathlib import Path

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import Contraindication
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

GUARD_JS = Path(__file__).resolve().parents[2] / "static" / "js" / "meso_dirty_guard.js"


@pytest.fixture
def link():
    return CoachAthleteFactory(
        coach=UserFactory(), athlete=UserFactory(name="Jordan Intake")
    )


def record_url(link):
    return reverse("meso:athlete_record", kwargs={"pk": link.athlete_id})


def profile_url(link):
    return reverse("meso:athlete", kwargs={"pk": link.athlete_id})


def texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def tags(body, name):
    """Every opening <input|textarea|select|button> tag carrying ``name="..."``."""
    return [
        t
        for t in re.findall(r"<(?:input|textarea|select|button)\b[^>]*>", body)
        if f'name="{name}"' in t
    ]


class TestProfileRendersOneForm:
    def test_single_intake_form_owns_every_field(self, client, link):
        client.force_login(link.coach)
        body = client.get(profile_url(link)).content.decode()

        assert body.count('id="athlete-intake"') == 1
        form_tag = re.search(r'<form[^>]*id="athlete-intake"[^>]*>', body).group(0)
        assert f'action="{record_url(link)}"' in form_tag
        assert "data-dirty-guard" in form_tag
        for name in (
            "label",
            "goals",
            "training_started",
            "notes",
            "unit",
            "new_contraindication",
            "add_contraindication",
        ):
            found = tags(body, name)
            assert found, f"no control named {name}"
            assert all('form="athlete-intake"' in t for t in found), (name, found)

    def test_old_per_section_buttons_are_gone(self, client, link):
        client.force_login(link.coach)
        body = client.get(profile_url(link)).content.decode()

        for gone in ("Save goals", "Save training history", "Save name"):
            assert gone not in body
        assert "Save athlete record" in body
        assert body.count("Save athlete record") == 1

    def test_training_started_blocks_future_dates(self, client, link):
        from django.utils import timezone

        client.force_login(link.coach)
        body = client.get(profile_url(link)).content.decode()
        (tag,) = tags(body, "training_started")
        assert f'max="{timezone.localdate():%Y-%m-%d}"' in tag

    def test_template_loads_the_dirty_guard(self, client, link):
        assert GUARD_JS.exists()
        client.force_login(link.coach)
        body = client.get(profile_url(link)).content.decode()
        assert "js/meso_dirty_guard.js" in body


class TestAthleteRecordSavesEverything:
    def test_all_fields_persist_with_one_message(self, client, link):
        client.force_login(link.coach)
        response = client.post(
            record_url(link),
            {
                "label": "  Jo\n Intake ",
                "goals": "Squat 315",
                "notes": "Old ankle sprain",
                "unit": "kg",
                "training_started": "2024-01-15",
            },
        )
        assert response.status_code == 302
        profile = AthleteProfile.objects.get(user=link.athlete)
        assert profile.goals == "Squat 315"
        assert profile.notes == "Old ankle sprain"
        assert profile.unit == "kg"
        assert str(profile.training_started) == "2024-01-15"
        link.refresh_from_db()
        assert link.label == "Jo Intake"
        assert texts(response) == ["Athlete record updated."]

    def test_contraindication_and_goals_persist_together(self, client, link):
        client.force_login(link.coach)
        response = client.post(
            record_url(link),
            {"new_contraindication": "L knee: no deep flexion", "goals": "Squat 315"},
        )
        assert response.status_code == 302
        assert Contraindication.objects.filter(
            athlete=link.athlete, text="L knee: no deep flexion", active=True
        ).exists()
        assert AthleteProfile.objects.get(user=link.athlete).goals == "Squat 315"
        assert texts(response) == [
            "Athlete record updated.",
            "Contraindication added.",
        ]

    def test_duplicate_contraindication_still_saves_goals(self, client, link):
        Contraindication.objects.create(athlete=link.athlete, text="L knee")
        client.force_login(link.coach)
        response = client.post(
            record_url(link),
            {"new_contraindication": "l knee", "goals": "Squat 315"},
        )
        assert AthleteProfile.objects.get(user=link.athlete).goals == "Squat 315"
        assert Contraindication.objects.filter(athlete=link.athlete).count() == 1
        assert "That contraindication is already active." in texts(response)

    def test_blank_contraindication_field_is_ignored(self, client, link):
        client.force_login(link.coach)
        response = client.post(
            record_url(link), {"new_contraindication": "  ", "goals": "Squat"}
        )
        assert AthleteProfile.objects.get(user=link.athlete).goals == "Squat"
        assert not Contraindication.objects.filter(athlete=link.athlete).exists()
        assert texts(response) == ["Athlete record updated."]

    def test_partial_post_leaves_other_fields_alone(self, client, link):
        AthleteProfile.objects.create(
            user=link.athlete, goals="old", notes="keep me", unit="kg"
        )
        client.force_login(link.coach)
        client.post(record_url(link), {"goals": "new goals"})
        profile = AthleteProfile.objects.get(user=link.athlete)
        assert profile.goals == "new goals"
        assert profile.notes == "keep me"
        assert profile.unit == "kg"
        link.refresh_from_db()
        assert link.label == ""

    def test_overlong_label_rejects_the_whole_post(self, client, link):
        # Decision: one form, one outcome. A rejected label rolls back the goals
        # typed beside it rather than half-applying the POST.
        client.force_login(link.coach)
        response = client.post(
            record_url(link),
            {"label": "x" * 256, "goals": "Squat 315"},
            follow=True,
        )
        # Redirected to the profile, which re-renders the typed values (#657/#680).
        assert response.status_code == 200
        assert not AthleteProfile.objects.filter(
            user=link.athlete, goals="Squat 315"
        ).exists()
        link.refresh_from_db()
        assert link.label == ""
        assert not any("Athlete record updated" in m for m in texts(response))
        assert any("255" in m for m in texts(response))

    @pytest.mark.parametrize("who", ["other_coach", "athlete", "anon"])
    def test_only_the_active_coach_can_post(self, client, link, who):
        if who == "other_coach":
            other = UserFactory()
            CoachAthleteFactory(coach=other, athlete=UserFactory())
            client.force_login(other)
        elif who == "athlete":
            client.force_login(link.athlete)
        response = client.post(record_url(link), {"goals": "hacked", "label": "hacked"})
        assert response.status_code in (302, 404)
        if who == "anon":
            assert response.status_code == 302
            assert "login" in response["Location"]
        assert not AthleteProfile.objects.filter(
            user=link.athlete, goals="hacked"
        ).exists()
        assert CoachAthlete.objects.get(pk=link.pk).label != "hacked"


@pytest.fixture
def coach(client):
    user = UserFactory(name="Old Name")
    CoachProfile.objects.create(user=user, display_name="Old Display")
    client.force_login(user)
    return user


SETTINGS = reverse("meso:settings")


class TestSettingsOneForm:
    def test_one_post_saves_name_and_coaching(self, client, coach):
        response = client.post(
            SETTINGS,
            {
                "name": "Maya Okonkwo",
                "display_name": "Coach Maya",
                "programming_style": "Compound-first, RPE",
                "avoid_rules": "No kipping",
                "unit": "kg",
            },
        )
        assert response.status_code == 302
        coach.refresh_from_db()
        assert coach.name == "Maya Okonkwo"
        profile = coach.coach_profile
        profile.refresh_from_db()
        assert profile.display_name == "Coach Maya"
        assert profile.programming_style == ["Compound-first", "RPE"]
        assert profile.avoid_rules == "No kipping"
        assert profile.default_unit == "kg"

    def test_invalid_coaching_field_saves_nothing_and_keeps_input(self, client, coach):
        too_many = ", ".join(f"tag{i}" for i in range(13))
        response = client.post(
            SETTINGS,
            {
                "name": "Typed New Name",
                "display_name": "Typed Display",
                "programming_style": too_many,
                "avoid_rules": "Typed rule",
                "unit": "kg",
            },
            follow=True,
        )
        assert response.status_code == 200
        coach.refresh_from_db()
        assert coach.name == "Old Name"
        assert coach.coach_profile.display_name == "Old Display"
        body = response.content.decode()
        assert "Typed New Name" in body
        assert "Typed Display" in body
        assert "Typed rule" in body
        assert "errorlist" in body

    def test_legacy_section_posts_still_work(self, client, coach):
        response = client.post(SETTINGS, {"section": "name", "name": "Legacy Name"})
        assert response.status_code == 302
        coach.refresh_from_db()
        assert coach.name == "Legacy Name"
        client.post(
            SETTINGS, {"section": "coaching", "display_name": "Legacy D", "unit": "kg"}
        )
        coach.coach_profile.refresh_from_db()
        assert coach.coach_profile.display_name == "Legacy D"

    def test_athlete_sectionless_post_saves_name_only(self, client):
        athlete = UserFactory(name="Before")
        CoachAthleteFactory(coach=UserFactory(), athlete=athlete)
        client.force_login(athlete)
        response = client.post(SETTINGS, {"name": "After", "display_name": "x"})
        assert response.status_code == 302
        athlete.refresh_from_db()
        assert athlete.name == "After"
        assert not CoachProfile.objects.filter(user=athlete).exists()

    def test_renders_one_guarded_form_and_one_submit(self, client, coach):
        body = client.get(SETTINGS).content.decode()
        assert body.count("data-dirty-guard") == 1
        assert body.count('type="submit"') == 1
        assert "Save settings" in body
        assert "js/meso_dirty_guard.js" in body
