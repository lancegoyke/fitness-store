"""The one journey that drives the real allauth login form (issue #506).

Every other journey in this suite cookie-logs-in via the `login()` fixture
(`Client().force_login()` + a copied session cookie) so it can open straight
on the page under test. This test is the exception — it proves the login
form itself actually works end to end, so nothing else needs to.
"""

import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db


def test_athlete_logs_in_and_reaches_training_home(
    page, viewport, shot, press, delivered_plan
):
    athlete = delivered_plan.athlete

    # Unauthenticated visit to the athlete's training home bounces to the
    # real login form (LoginRequiredMixin appends ?next=/meso/me/).
    page.goto("/meso/me/")
    expect(page).to_have_url(re.compile(r"/accounts/login/"))
    expect(page.get_by_role("heading", name="Login to your account")).to_be_visible()
    shot("01-login-form")

    page.locator('input[name="login"]').fill(athlete.email)
    page.locator('input[name="password"]').fill("testpass123")
    press(page.get_by_role("button", name="Sign In"))

    # allauth honors the ?next= from the bounce, so this lands back on
    # /meso/me/ rather than LOGIN_REDIRECT_URL (/users/profile/). The
    # delivered "Lower" session shows up as a card link (there's also a
    # read-only "Your block" grid further down that repeats the day's name
    # in plain text, so scope to the link specifically).
    expect(page).to_have_url(re.compile(r"/meso/me/$"))
    expect(page.get_by_role("link", name=re.compile("Lower"))).to_be_visible()
    shot("02-training-home")
