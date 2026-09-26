"""Behavior coverage for the coach-written athlete record (#603)."""

from datetime import timedelta

import pytest
from django.urls import NoReverseMatch
from django.urls import reverse
from django.utils import timezone

from store_project.meso.agent import service
from store_project.meso.factories import AthleteProfileFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AthleteProfile
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import Contraindication
from store_project.meso.models import Plan
from store_project.meso.serializers import serialize_athlete_identity
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def route(name, **kwargs):
    """Turn a missing #603 route on pristine main into an assertion failure."""
    try:
        return reverse(f"meso:{name}", kwargs=kwargs)
    except NoReverseMatch:
        pytest.fail(f"The meso:{name} route is missing")


def previous_month_start():
    this_month = timezone.localdate().replace(day=1)
    return (this_month - timedelta(days=1)).replace(day=1)


class TestAthleteRecord:
    def test_round_trip_partial_update_validation_and_escaping(self, client):
        link = CoachAthleteFactory(
            coach=UserFactory(), athlete=UserFactory(name="Jordan Record")
        )
        client.force_login(link.coach)
        url = route("athlete_record", pk=link.athlete_id)
        started = previous_month_start()

        response = client.post(
            url,
            {
                "goals": "  Build strength\nRun pain-free  ",
                "training_started": started.isoformat(),
                "notes": "  Old ankle sprain\nPrefers mornings  ",
            },
        )
        assert response.status_code == 302
        profile = AthleteProfile.objects.get(user=link.athlete)
        assert profile.goals == "Build strength\nRun pain-free"
        assert profile.training_started == started
        assert profile.notes == "Old ankle sprain\nPrefers mornings"

        profile_body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete_id})
        ).content.decode()
        roster_body = client.get(reverse("meso:roster")).content.decode()
        assert "Build strength" in profile_body
        assert "Run pain-free" in profile_body
        assert "Old ankle sprain" in profile_body
        assert "1 mo training" in profile_body
        assert "1 mo training" in roster_body

        unsafe = '<script>alert("x")</script>\nStill train'
        client.post(url, {"goals": unsafe})
        profile.refresh_from_db()
        assert profile.goals == unsafe
        assert profile.notes == "Old ankle sprain\nPrefers mornings"
        assert profile.training_started == started
        escaped_body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete_id})
        ).content.decode()
        assert '<script>alert("x")</script>' not in escaped_body
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in escaped_body

        future = timezone.localdate() + timedelta(days=1)
        client.post(
            url,
            {
                "goals": "Must not save",
                "training_started": future.isoformat(),
                "notes": "Must not save either",
            },
        )
        profile.refresh_from_db()
        assert profile.goals == unsafe
        assert profile.notes == "Old ankle sprain\nPrefers mornings"
        assert profile.training_started == started

        client.post(url, {"goals": "Still no", "training_started": "not-a-date"})
        profile.refresh_from_db()
        assert profile.goals == unsafe
        assert profile.training_started == started

    @pytest.mark.parametrize("delivered", [False, True])
    def test_athlete_goals_and_the_plans_goal_are_both_shown(self, client, delivered):
        # The profile overlays program data onto the athlete record; the plan's
        # ``goal`` must not replace what the coach wrote as the athlete's goals.
        link = CoachAthleteFactory(coach=UserFactory(), athlete=UserFactory())
        plan = PlanFactory(
            relationship=link,
            goal="Bench 315",
            status=Plan.Status.ACTIVE if delivered else Plan.Status.DRAFT,
        )
        if delivered:
            WeekFactory(mesocycle=MesocycleFactory(plan=plan, order=0), index=1)
        AthleteProfileFactory(user=link.athlete, goals="Run a marathon")
        client.force_login(link.coach)
        body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete_id})
        ).content.decode()
        assert "Run a marathon" in body
        assert "Program goal: Bench 315" in body

    def test_goals_field_defaults_to_empty_string(self):
        assert hasattr(AthleteProfile, "goals"), "AthleteProfile.goals is missing"
        assert AthleteProfile(user=UserFactory()).goals == ""


class TestContraindications:
    def test_add_clear_duplicate_reactivate_and_downstream_surfaces(self, client):
        link = CoachAthleteFactory()
        plan = PlanFactory(relationship=link)
        client.force_login(link.coach)
        add_url = route("athlete_contraindication_add", pk=link.athlete_id)

        response = client.post(
            add_url, {"text": "  L knee \n — avoid   deep flexion  "}
        )
        assert response.status_code == 302
        row = Contraindication.objects.get(athlete=link.athlete)
        assert row.text == "L knee — avoid deep flexion"
        assert row.active is True
        profile_body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete_id})
        ).content.decode()
        assert row.text in profile_body
        assert serialize_athlete_identity(plan)["contraindications"] == [
            {"label": "L knee", "text": row.text}
        ]
        assert service.build_context(plan, None)["athlete"]["contraindications"] == [
            row.text
        ]

        client.post(add_url, {"text": "l KNEE — AVOID DEEP FLEXION"})
        assert Contraindication.objects.filter(athlete=link.athlete).count() == 1

        clear_url = route(
            "athlete_contraindication_clear", pk=link.athlete_id, cid=row.pk
        )
        client.post(clear_url)
        row.refresh_from_db()
        assert row.active is False
        profile_body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete_id})
        ).content.decode()
        assert row.text not in profile_body
        assert serialize_athlete_identity(plan)["contraindications"] == []
        assert service.build_context(plan, None)["athlete"]["contraindications"] == []

        client.post(add_url, {"text": "L KNEE — AVOID DEEP FLEXION"})
        row.refresh_from_db()
        assert row.active is True
        assert Contraindication.objects.filter(athlete=link.athlete).count() == 1

        client.post(add_url, {"text": "   "})
        client.post(add_url, {"text": "x" * 256})
        assert Contraindication.objects.filter(athlete=link.athlete).count() == 1

    def test_clear_rejects_a_contraindication_owned_by_another_athlete(self, client):
        link = CoachAthleteFactory()
        other = Contraindication.objects.create(
            athlete=UserFactory(), text="Not this athlete"
        )
        client.force_login(link.coach)
        response = client.post(
            route(
                "athlete_contraindication_clear",
                pk=link.athlete_id,
                cid=other.pk,
            )
        )
        assert response.status_code == 404
        other.refresh_from_db()
        assert other.active is True


class TestRecordAccess:
    def test_only_the_active_coach_can_write_and_get_is_rejected(self, client):
        link = CoachAthleteFactory()
        contraindication = Contraindication.objects.create(
            athlete=link.athlete, text="Existing"
        )
        targets = [
            (route("athlete_record", pk=link.athlete_id), {"goals": "Nope"}),
            (
                route("athlete_contraindication_add", pk=link.athlete_id),
                {"text": "Nope"},
            ),
            (
                route(
                    "athlete_contraindication_clear",
                    pk=link.athlete_id,
                    cid=contraindication.pk,
                ),
                {},
            ),
        ]

        client.force_login(link.coach)
        for url, _ in targets:
            assert client.get(url).status_code == 405

        for intruder in (UserFactory(), link.athlete):
            client.force_login(intruder)
            for url, payload in targets:
                assert client.post(url, payload).status_code == 404

        client.logout()
        for url, payload in targets:
            response = client.post(url, payload)
            assert response.status_code == 302
            assert reverse("account_login") in response.url

        client.force_login(link.coach)
        link.end()
        for url, payload in targets:
            assert client.post(url, payload).status_code == 404
        assert not AthleteProfile.objects.filter(user=link.athlete).exists()
        contraindication.refresh_from_db()
        assert contraindication.active is True
        assert Contraindication.objects.filter(athlete=link.athlete).count() == 1


class TestCoachingSettings:
    def test_style_and_avoid_rules_round_trip_to_athlete_profile(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)
        response = client.post(
            reverse("meso:settings"),
            {
                "section": "coaching",
                "display_name": "Coach Jo",
                "programming_style": (
                    " Compound-first, RPE-based load, compound-FIRST, Mobility "
                ),
                "avoid_rules": "  Machine-only days\nUntracked progressions  ",
            },
        )
        assert response.status_code == 302
        profile = CoachProfile.objects.get(user=link.coach)
        assert profile.programming_style == [
            "Compound-first",
            "RPE-based load",
            "Mobility",
        ]
        assert profile.avoid_rules == "Machine-only days\nUntracked progressions"

        body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete_id})
        ).content.decode()
        assert "Compound-first" in body
        assert "RPE-based load" in body
        assert "Avoid:</span> Machine-only days" in body

    def test_empty_style_has_settings_link_and_no_bare_avoid_label(self, client):
        link = CoachAthleteFactory()
        CoachProfile.objects.create(user=link.coach)
        client.force_login(link.coach)
        body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete_id})
        ).content.decode()
        assert "Your style isn't set yet." in body
        assert f'href="{reverse("meso:settings")}"' in body
        assert "Avoid:</span>" not in body

    @pytest.mark.parametrize(
        ("style", "error"),
        [
            (", ".join(f"tag-{index}" for index in range(13)), "no more than 12"),
            ("x" * 41, "40 characters or fewer"),
        ],
    )
    def test_invalid_style_tags_are_form_errors(self, client, style, error):
        link = CoachAthleteFactory()
        profile = CoachProfile.objects.create(
            user=link.coach, programming_style=["Existing"]
        )
        client.force_login(link.coach)
        response = client.post(
            reverse("meso:settings"),
            {
                "section": "coaching",
                "display_name": "Coach",
                "programming_style": style,
                "avoid_rules": "Keep this",
            },
        )
        assert response.status_code == 200
        assert error in response.content.decode()
        profile.refresh_from_db()
        assert profile.programming_style == ["Existing"]
        assert profile.avoid_rules == ""

    def test_plain_athlete_cannot_see_or_create_coaching_settings(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.athlete)
        body = client.get(reverse("meso:settings")).content.decode()
        assert "Programming style" not in body
        response = client.post(
            reverse("meso:settings"),
            {
                "section": "coaching",
                "display_name": "Not a coach",
                "programming_style": "Nope",
                "avoid_rules": "Nope",
            },
        )
        assert response.status_code == 404
        assert not CoachProfile.objects.filter(user=link.athlete).exists()


class TestEndRelationship:
    def test_normal_link_can_end_but_self_and_demo_links_hide_the_form(self, client):
        coach = UserFactory()
        normal = CoachAthleteFactory(
            coach=coach, athlete=UserFactory(name="Past Athlete")
        )
        self_link = CoachAthleteFactory(coach=coach, athlete=coach, is_self=True)
        demo = CoachAthleteFactory(coach=coach, is_demo=True)
        client.force_login(coach)

        normal_url = reverse("meso:relationship_end", kwargs={"token": normal.token})
        normal_body = client.get(
            reverse("meso:athlete", kwargs={"pk": normal.athlete_id})
        ).content.decode()
        assert f'action="{normal_url}"' in normal_body
        assert "End coaching relationship" in normal_body
        assert "They move to Past athletes; their programs are archived" in normal_body

        for hidden in (self_link, demo):
            body = client.get(
                reverse("meso:athlete", kwargs={"pk": hidden.athlete_id})
            ).content.decode()
            hidden_url = reverse(
                "meso:relationship_end", kwargs={"token": hidden.token}
            )
            assert f'action="{hidden_url}"' not in body

        response = client.post(normal_url)
        assert response.status_code == 302
        normal.refresh_from_db()
        assert normal.status == CoachAthlete.Status.ENDED
        history = client.get(reverse("meso:relationship_history")).content.decode()
        assert "Past Athlete" in history


class TestAgentAthleteRecord:
    def test_context_includes_profile_fields_and_empty_defaults(self):
        profile = AthleteProfileFactory(
            goals="Build a 10K base",
            notes="Prefers three sessions weekly",
            training_started=previous_month_start(),
        )
        plan = PlanFactory(relationship=CoachAthleteFactory(athlete=profile.user))
        athlete = service.build_context(plan, None)["athlete"]
        assert athlete["goals"] == "Build a 10K base"
        assert athlete["notes"] == "Prefers three sessions weekly"
        assert athlete["training_started"] == profile.training_started.isoformat()

        empty_plan = PlanFactory(
            relationship=CoachAthleteFactory(athlete=UserFactory())
        )
        empty = service.build_context(empty_plan, None)["athlete"]
        assert empty["goals"] == ""
        assert empty["notes"] == ""
        assert empty["training_started"] is None
