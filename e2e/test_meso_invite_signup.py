"""Onboarding through signup and login (UAT round 2: #642, #644, #641).

Three journeys, each driven end to end through the real allauth forms:

1. An invited stranger follows the claim link, creates an account (name and
   email prefilled, store chrome hidden) and lands on the athlete home already
   accepted — no second Accept button.
2. An existing user follows the claim link logged out, signs in, and ends up
   the same way; their program leads the page and at most one prompt shows.
3. A coach who chose the free trial on the marketing page signs up and lands
   on the roster with the trial running, without being asked to pick again.
"""

import re
import secrets

import pytest
from playwright.sync_api import expect
from store_project.meso.models import CoachInvite
from store_project.users.factories import UserFactory

from ._layout import assert_fits

pytestmark = pytest.mark.django_db

# Random per run: clears AUTH_PASSWORD_VALIDATORS and keeps a credential-shaped
# literal out of the repo.
PASSWORD = secrets.token_urlsafe(16)

ATHLETE_HOME = re.compile(r"/meso/me/$")


@pytest.fixture
def sam():
    return UserFactory(name="Sam Rivera", email="sam.rivera@example.com")


def _invite(coach, email, label=""):
    invite, _ = CoachInvite.open_for(coach=coach, email=email, label=label)
    return invite


def _claim_path(invite):
    return f"/meso/claim/{invite.token}/"


def _visible_prompts(page):
    return page.locator("[data-prompt-priority]:visible").count()


def _assert_accepted_home(page, coach_name):
    expect(page).to_have_url(ATHLETE_HOME)
    expect(page.get_by_text(f"You're now training with {coach_name}.")).to_be_visible()
    # Accepted on arrival: no second Accept anywhere on the page.
    expect(page.get_by_role("button", name="Accept")).to_have_count(0)
    coaches = page.get_by_test_id("your-coaches")
    expect(coaches).to_be_visible()
    expect(coaches.get_by_text(coach_name)).to_be_visible()


def test_invited_stranger_signs_up_and_lands_accepted(page, viewport, shot, press, sam):
    invite = _invite(sam, "jordan@example.com", label="Jordan Ellis")

    page.goto(_claim_path(invite))
    expect(page.get_by_text("invited you to train").first).to_be_visible()
    shot("01-claim")
    press(page.get_by_role("link", name="Create account"))

    expect(page.get_by_role("heading", name="Join Sam Rivera on Meso")).to_be_visible()
    form = page.locator("#email-form")
    expect(form.locator("#id_name")).to_have_value("Jordan Ellis")
    expect(form.locator("#id_email")).to_have_value("jordan@example.com")
    # Store chrome is gone: no nav, no newsletter signup.
    expect(page.locator(".nav")).to_have_count(0)
    assert "newsletter" not in page.content().lower()
    shot("02-signup")

    form.locator("#id_password1").fill(PASSWORD)
    press(page.get_by_role("button", name="Sign Up"))

    _assert_accepted_home(page, "Sam Rivera")
    # The request form stays folded away once a coach is on the page.
    expect(page.locator("details", has_text="Request a coach")).not_to_have_attribute(
        "open", re.compile(".*")
    )
    expect(page.get_by_text("Are you a coach?")).to_have_count(0)
    assert _visible_prompts(page) <= 1
    if viewport["is_phone"]:
        assert_fits(page)
    shot("03-home")


def test_existing_user_signs_in_from_claim_link(
    page, viewport, shot, press, sam, delivered_plan
):
    athlete = delivered_plan.athlete
    invite = _invite(sam, athlete.email)
    # `delivered_plan`'s athlete uses the factory password.
    page.goto(_claim_path(invite))
    expect(page.get_by_text("invited you to train").first).to_be_visible()
    press(page.get_by_role("link", name="Sign in"))

    page.locator('input[name="login"]').fill(athlete.email)
    page.locator('input[name="password"]').fill("testpass123")
    press(page.get_by_role("button", name="Sign In"))

    _assert_accepted_home(page, "Sam Rivera")

    # Their program leads the page; prompts come after it, never before.
    program = page.get_by_role("link", name=re.compile("Lower")).first
    expect(program).to_be_visible()
    coaches_top = page.get_by_test_id("your-coaches").bounding_box()["y"]
    assert program.bounding_box()["y"] < coaches_top
    for prompt in page.locator("[data-prompt-priority]:visible").all():
        assert prompt.bounding_box()["y"] > program.bounding_box()["y"]
    assert _visible_prompts(page) <= 1
    if viewport["is_phone"]:
        assert_fits(page)
    shot("01-home")


def test_coach_trial_survives_signup(page, viewport, shot, press):
    page.goto("/meso/coach/?plan=trial")
    press(page.get_by_role("link", name="Create your account"))

    form = page.locator("#email-form")
    form.locator("#id_name").fill("Pat Coach")
    form.locator("#id_email").fill("pat.coach@example.com")
    form.locator("#id_password1").fill(PASSWORD)
    press(page.get_by_role("button", name="Sign Up"))

    expect(page).to_have_url(re.compile(r"/meso/$"))
    expect(page.get_by_text("Your 14-day free trial has started")).to_be_visible()
    # Not asked to choose a plan again.
    expect(page.get_by_role("button", name=re.compile("Start .*trial"))).to_have_count(
        0
    )
    shot("01-roster")
