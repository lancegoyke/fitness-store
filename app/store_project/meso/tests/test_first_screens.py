from datetime import timedelta
from urllib.parse import quote

import pytest
from django.contrib import admin
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from store_project.meso.admin import LoggedSetInline
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog
from store_project.meso.models import Unit
from store_project.users.factories import SuperAdminFactory
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("plan", ["trial", "free"])
def test_signup_carries_coach_plan_intent(client, plan):
    become_url = reverse("meso:become_coach")
    client.get(f"{become_url}?plan={plan}")

    response = client.post(
        f"{reverse('account_signup')}?next={quote(become_url)}",
        {"email": f"{plan}@example.com", "password1": "safe password 123"},
        follow=True,
    )

    user = response.wsgi_request.user
    assert user.is_authenticated
    assert CoachProfile.objects.filter(user=user).exists()
    assert response.redirect_chain[-1][0] == reverse("meso:roster")
    if plan == "trial":
        assert (
            CoachSubscription.objects.get(coach=user).status
            == CoachSubscription.Status.TRIALING
        )
    else:
        assert not CoachSubscription.objects.filter(coach=user).exists()


def test_no_intent_shows_trial_as_first_primary_choice(client):
    user = UserFactory()
    client.force_login(user)

    body = client.get(reverse("meso:become_coach")).content.decode()

    trial = body.index("Start 14-day free trial")
    free = body.index("Start coaching free")
    assert trial < free
    assert 'value="trial" class="meso-btn meso-btn--primary"' in body
    assert not CoachProfile.objects.filter(user=user).exists()


def test_authenticated_query_parameter_does_not_start_trial(client):
    user = UserFactory()
    client.force_login(user)

    response = client.get(f"{reverse('meso:become_coach')}?plan=trial")

    assert response.status_code == 200
    assert not CoachProfile.objects.filter(user=user).exists()
    assert not CoachSubscription.objects.filter(coach=user).exists()


def test_anonymous_pending_invite_has_signup_and_login_claim_links(client):
    coach = UserFactory(name="Coach Rivera")
    invite, _ = CoachInvite.open_for(coach=coach, email="athlete@example.com")
    claim_path = reverse("meso:invite_claim", kwargs={"token": invite.token})

    response = client.get(claim_path)
    body = response.content.decode()

    assert response.status_code == 200
    assert "Coach Rivera invited you to train" in body
    assert f"{reverse('account_signup')}?next={quote(claim_path)}" in body
    assert f"{reverse('account_login')}?next={quote(claim_path)}" in body
    assert "Create account" in body
    assert "meso-btn meso-btn--primary" in body
    assert "<form" not in body


@pytest.mark.parametrize("state", ["unknown", "used", "expired", "post"])
def test_anonymous_unclaimable_invite_redirects_to_login(client, state):
    coach = UserFactory()
    invite, _ = CoachInvite.open_for(coach=coach, email="athlete@example.com")
    if state == "unknown":
        token = "00000000-0000-0000-0000-000000000000"
    else:
        token = invite.token
    if state == "used":
        invite.status = CoachInvite.Status.DECLINED
        invite.save(update_fields=["status"])
    elif state == "expired":
        invite.expires_at = timezone.now() - timedelta(seconds=1)
        invite.save(update_fields=["expires_at"])
    path = reverse("meso:invite_claim", kwargs={"token": token})

    response = client.post(path) if state == "post" else client.get(path)

    assert response.status_code == 302
    assert response.url == f"{reverse('account_login')}?next={quote(path)}"


def test_landing_has_only_shared_nav(client):
    body = client.get(reverse("meso:roster")).content.decode()
    login_url = reverse("account_login")

    assert "meso-topnav" not in body
    header = body[body.index('<header class="nav"') : body.index("</header>")]
    assert header.count(f'href="{login_url}"') == 1
    assert 'class="nav"' in client.get(reverse("products:store")).content.decode()


def test_roster_names_pending_invite_as_blocker(client):
    coach = UserFactory()
    CoachProfile.objects.create(user=coach)
    CoachInvite.open_for(coach=coach, email="jordan.ellis@example.com", label="Jordan")
    client.force_login(coach)

    body = client.get(reverse("meso:roster")).content.decode()

    assert body.count("1 invite pending") == 2
    assert "No athletes yet — Jordan hasn't accepted your invite yet." in body
    assert "+ Add yourself as an athlete" in body
    assert "Invite an athlete first to build them a program." not in body


def test_roster_without_invite_keeps_original_empty_copy(client):
    coach = UserFactory()
    CoachProfile.objects.create(user=coach)
    client.force_login(coach)

    body = client.get(reverse("meso:roster")).content.decode()

    assert "No athletes yet. Invite one to get started." in body
    assert "Invite an athlete first to build them a program." in body
    assert "invite pending" not in body


def test_coach_profile_defaults_to_pounds_without_changing_explicit_kg():
    pounds = CoachProfile.objects.create(user=UserFactory())
    kilograms = CoachProfile.objects.create(
        user=UserFactory(), default_unit=Unit.KILOGRAMS
    )

    assert pounds.default_unit == Unit.POUNDS
    assert kilograms.default_unit == Unit.KILOGRAMS


@pytest.mark.parametrize("unit", [Unit.POUNDS, Unit.KILOGRAMS])
def test_admin_logged_set_inline_derives_new_row_unit_from_plan(unit):
    plan = PlanFactory(unit=unit)
    mesocycle = MesocycleFactory(plan=plan)
    week = WeekFactory(mesocycle=mesocycle)
    session = day(week)
    prescription = presc(session)
    session_log = SessionLogFactory(session=session, athlete=plan.relationship.athlete)
    inline = LoggedSetInline(SessionLog, admin.site)
    request = RequestFactory().post("/admin/")
    request.user = SuperAdminFactory()
    formset_class = inline.get_formset(request, session_log)
    prefix = formset_class.get_default_prefix()
    data = {
        f"{prefix}-TOTAL_FORMS": "1",
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
        f"{prefix}-0-prescription": str(prescription.pk),
        f"{prefix}-0-set_number": "1",
        f"{prefix}-0-reps": "5",
        f"{prefix}-0-load": "100",
        f"{prefix}-0-rpe": "8",
    }
    formset = formset_class(data=data, instance=session_log, prefix=prefix)

    assert formset.is_valid(), formset.errors
    formset.save()
    assert LoggedSet.objects.get(session_log=session_log).unit == unit
