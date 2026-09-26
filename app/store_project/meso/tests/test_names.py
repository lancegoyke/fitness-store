"""Behavior coverage for Meso's account names and relationship labels (#602)."""

import pytest
from django.core import mail
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachProfile
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed_plan(*, coach=None, athlete=None, label=""):
    # ``label`` is only passed when set, so tests that don't use it can also run
    # against a checkout that predates the field and fail on their assertions.
    link = CoachAthleteFactory(
        coach=coach or UserFactory(),
        athlete=athlete or UserFactory(),
        **({"label": label} if label else {}),
    )
    plan = PlanFactory(relationship=link, title="Foundation", status=Plan.Status.ACTIVE)
    mesocycle = MesocycleFactory(plan=plan, name="Base", order=0)
    WeekFactory(mesocycle=mesocycle, index=1)
    return plan


def deliver(client, plan, django_capture_on_commit_callbacks):
    client.force_login(plan.coach)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})
        )
    assert response.status_code == 201
    return mail.outbox[-1]


class TestCoachNames:
    def test_invite_email_uses_account_name_then_coach_alias(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory(name="Maya Okonkwo", email="coach.uat@example.com")
        client.force_login(coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("meso:coach_invite"), {"email": "first@example.com"})
        assert mail.outbox[0].subject.startswith("Maya Okonkwo invited you")
        assert "Maya Okonkwo" in mail.outbox[0].body
        assert "coach.uat" not in mail.outbox[0].body

        CoachProfile.objects.create(user=coach, display_name="Coach Maya")
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("meso:coach_invite"), {"email": "second@example.com"})
        assert mail.outbox[1].subject.startswith("Coach Maya invited you")

    @pytest.mark.parametrize("alias", ["", "Coach Maya"])
    def test_athlete_home_names_the_coach(self, client, alias):
        coach = UserFactory(name="Maya Okonkwo")
        if alias:
            CoachProfile.objects.create(user=coach, display_name=alias)
        athlete = UserFactory()
        seed_plan(coach=coach, athlete=athlete)
        client.force_login(athlete)
        body = client.get(reverse("meso:athlete_home")).content.decode()
        assert f"Coach {alias or 'Maya Okonkwo'}" in body

    def test_claim_page_names_coach_in_heading_and_body(self, client):
        coach = UserFactory(name="Maya Okonkwo")
        CoachProfile.objects.create(user=coach, display_name="Coach Maya")
        invite, _ = CoachInvite.open_for(coach=coach, email="athlete@example.com")
        client.force_login(UserFactory())
        body = client.get(
            reverse("meso:invite_claim", kwargs={"token": invite.token})
        ).content.decode()
        assert "Coach Maya invited you to train" in body
        assert "training with Coach Maya" in body


class TestDeliveryNames:
    def test_own_name_beats_label_and_email_prefix(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory(name="Maya Okonkwo")
        CoachProfile.objects.create(user=coach, display_name="Coach Maya")
        athlete = UserFactory(name="Jordan Ellis", email="jordan.uat@example.com")
        email = deliver(
            client,
            seed_plan(coach=coach, athlete=athlete, label="Coach's guess"),
            django_capture_on_commit_callbacks,
        )
        assert "Hi Jordan Ellis," in email.body
        assert "Coach Maya just delivered" in email.body

    def test_relationship_label_is_the_unnamed_athletes_greeting(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory(name="Maya Okonkwo")
        athlete = UserFactory(name="", email="jordan.uat@example.com")
        plan = seed_plan(coach=coach, athlete=athlete)
        client.force_login(coach)
        response = client.post(
            reverse("meso:athlete_label", kwargs={"pk": athlete.pk}),
            {"label": "  Jordan\n\tEllis  "},
        )
        assert response.status_code == 302
        plan.relationship.refresh_from_db()
        assert plan.relationship.label == "Jordan Ellis"

        email = deliver(client, plan, django_capture_on_commit_callbacks)
        assert "Hi Jordan Ellis," in email.body

    def test_email_prefix_is_the_final_greeting_fallback(
        self, client, django_capture_on_commit_callbacks
    ):
        athlete = UserFactory(name="", email="jordan.uat@example.com")
        email = deliver(
            client,
            seed_plan(coach=UserFactory(name="Maya"), athlete=athlete),
            django_capture_on_commit_callbacks,
        )
        assert "Hi jordan.uat," in email.body

    def test_plain_text_names_are_not_html_escaped(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory(name="Maya O'Brien & Co")
        client.force_login(coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("meso:coach_invite"), {"email": "invitee@example.com"})
        invite_email = mail.outbox[-1]
        assert "Maya O'Brien & Co" in invite_email.subject
        assert "Maya O'Brien & Co" in invite_email.body
        assert "&#x27;" not in invite_email.body
        assert "&amp;" not in invite_email.body

        athlete = UserFactory(name="Jordan O'Brien & Co")
        delivery = deliver(
            client,
            seed_plan(coach=coach, athlete=athlete),
            django_capture_on_commit_callbacks,
        )
        assert "Hi Jordan O'Brien & Co," in delivery.body
        assert "&#x27;" not in delivery.body
        assert "&amp;" not in delivery.body


class TestRelationshipLabels:
    def test_precedence_on_roster_and_profile(self, client):
        coach = UserFactory()
        own_name = UserFactory(name="Athlete Choice", email="own@example.com")
        labeled = UserFactory(name="", email="fallback.person@example.com")
        CoachAthleteFactory(coach=coach, athlete=own_name, label="Coach Guess")
        CoachAthleteFactory(coach=coach, athlete=labeled, label="Label Choice")
        client.force_login(coach)

        roster = client.get(reverse("meso:roster")).content.decode()
        assert "Athlete Choice" in roster
        assert "Coach Guess" not in roster
        assert "Label Choice" in roster
        assert roster.index("Athlete Choice") < roster.index("Label Choice")

        profile = client.get(
            reverse("meso:athlete", kwargs={"pk": labeled.pk})
        ).content.decode()
        assert '<h1 class="meso-h1">Label Choice</h1>' in profile
        own_profile = client.get(
            reverse("meso:athlete", kwargs={"pk": own_name.pk})
        ).content.decode()
        assert '<h1 class="meso-h1">Athlete Choice</h1>' in own_profile

    def test_label_is_private_per_coach_and_can_be_cleared(self, client):
        athlete = UserFactory(name="", email="shared.person@example.com")
        first = UserFactory()
        second = UserFactory()
        first_link = CoachAthleteFactory(coach=first, athlete=athlete)
        CoachAthleteFactory(coach=second, athlete=athlete)

        client.force_login(first)
        label_url = reverse("meso:athlete_label", kwargs={"pk": athlete.pk})
        client.post(label_url, {"label": "  Private\n Name  "})
        first_link.refresh_from_db()
        assert first_link.label == "Private Name"
        assert "Private Name" in client.get(reverse("meso:roster")).content.decode()

        client.force_login(second)
        second_roster = client.get(reverse("meso:roster")).content.decode()
        assert "Private Name" not in second_roster
        assert "shared.person" in second_roster

        client.force_login(first)
        client.post(label_url, {"label": ""})
        first_link.refresh_from_db()
        assert first_link.label == ""

    def test_label_endpoint_authorization_and_method(self, client):
        athlete = UserFactory(name="")
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, athlete=athlete)
        url = reverse("meso:athlete_label", kwargs={"pk": athlete.pk})
        client.force_login(coach)
        assert client.get(url).status_code == 405

        client.force_login(UserFactory())
        assert client.post(url, {"label": "Nope"}).status_code == 404

        pending_coach = UserFactory()
        CoachAthlete.invite(coach=pending_coach, athlete=athlete)
        client.force_login(pending_coach)
        assert client.post(url, {"label": "Nope"}).status_code == 404

    def test_roster_orders_by_resolved_name(self, client):
        coach = UserFactory()
        CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(name="", email="aaa@example.com"),
            label="Zed",
        )
        CoachAthleteFactory(
            coach=coach,
            athlete=UserFactory(name="Alice", email="zzz@example.com"),
            label="",
        )
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert body.index("Alice") < body.index("Zed")


class TestInviteLabels:
    def test_invite_label_round_trip_and_outstanding_updates(
        self, client, django_capture_on_commit_callbacks
    ):
        coach = UserFactory()
        client.force_login(coach)
        url = reverse("meso:coach_invite")
        with django_capture_on_commit_callbacks(execute=True):
            client.post(
                url,
                {"email": "new.athlete@example.com", "name": "  Jordan  Ellis "},
            )
        invite = CoachInvite.objects.get(coach=coach)
        assert invite.label == "Jordan Ellis"
        assert "Jordan Ellis" in client.get(reverse("meso:roster")).content.decode()

        with django_capture_on_commit_callbacks(execute=True):
            client.post(url, {"email": "new.athlete@example.com", "name": "J. Ellis"})
        invite.refresh_from_db()
        assert invite.label == "J. Ellis"
        with django_capture_on_commit_callbacks(execute=True):
            client.post(url, {"email": "new.athlete@example.com", "name": ""})
        invite.refresh_from_db()
        assert invite.label == "J. Ellis"

        athlete = UserFactory(name="", email="claimant@example.com")
        client.force_login(athlete)
        client.post(
            reverse("meso:invite_claim", kwargs={"token": invite.token}),
            {"action": "accept"},
        )
        link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
        assert link.label == "J. Ellis"
        client.force_login(coach)
        assert "J. Ellis" in client.get(reverse("meso:roster")).content.decode()

    def test_new_fields_default_to_empty_string(self):
        link = CoachAthleteFactory()
        invite = CoachInvite.objects.create(
            coach=UserFactory(), email="default-label@example.com"
        )
        assert link.label == ""
        assert invite.label == ""


class TestMesoSettings:
    def test_chip_links_to_settings_and_names_save(self, client):
        coach = UserFactory(name="Old Name")
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)
        roster = client.get(reverse("meso:roster")).content.decode()
        assert f'href="{reverse("meso:settings")}"' in roster

        response = client.post(
            reverse("meso:settings"),
            {"section": "name", "name": "  Maya\n Okonkwo "},
        )
        assert response.status_code == 302
        coach.refresh_from_db()
        assert coach.name == "Maya Okonkwo"

        client.post(
            reverse("meso:settings"),
            {"section": "coaching", "display_name": "  Coach\tMaya "},
        )
        coach.coach_profile.refresh_from_db()
        assert coach.coach_profile.display_name == "Coach Maya"
        body = client.get(reverse("meso:settings")).content.decode()
        assert "Athletes see you as: <strong>Coach Maya</strong>" in body

    def test_plain_athlete_cannot_create_a_coach_profile(self, client):
        coach = UserFactory()
        athlete = UserFactory()
        CoachAthleteFactory(coach=coach, athlete=athlete)
        client.force_login(athlete)
        body = client.get(reverse("meso:settings")).content.decode()
        assert "Coaching" not in body
        response = client.post(
            reverse("meso:settings"),
            {"section": "coaching", "display_name": "Not a coach"},
        )
        assert response.status_code == 404
        assert not CoachProfile.objects.filter(user=athlete).exists()
