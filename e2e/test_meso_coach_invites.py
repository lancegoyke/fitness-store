"""Coach journeys: inviting an athlete by email (issue #506, second slice).

Journey 2 drives both sides of a real email invite through the browser: the
coach's roster form and, in a second browser context with its own cookies
(``new_page``), a brand-new person with no account following the emailed
link. The claim link is pulled out of ``django.core.mail.outbox`` (the
in-process locmem backend), never read off the ``CoachInvite`` row directly —
the point is to prove the email a real invitee gets actually works. Two bugs
stop the full journey today (#522, #523), so it's a strict xfail, and a
second test covers the half that works.

Journey 3 covers the free-tier seat cap: a coach already at their one free
athlete can't open a second invite.
"""

import re
import secrets
from urllib.parse import urlparse

import pytest
from django.core import mail
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachSubscription
from store_project.meso.views import SEAT_LIMIT_MESSAGE

pytestmark = pytest.mark.django_db

# A brand-new person with no account. Both journey-2 tests use the same
# address — each test gets its own fresh database, so there's no collision.
INVITEE_EMAIL = "jordan.new@example.com"
# Random per run: clears every validator in AUTH_PASSWORD_VALIDATORS, and
# keeps a credential-shaped literal out of the repo (secret scanners flag one).
INVITEE_PASSWORD = secrets.token_urlsafe(16)


@pytest.fixture
def subscribed_coach(delivered_plan):
    """`delivered_plan` with the coach on an active subscription.

    Journey 2 needs a coach who isn't blocked by the free one-athlete cap
    (`delivered_plan` already has one active athlete) — journey 3 is what
    exercises that cap, so it uses `delivered_plan` alone.
    """
    CoachSubscriptionFactory(
        coach=delivered_plan.coach, status=CoachSubscription.Status.ACTIVE
    )
    return delivered_plan


# ---------------------------------------------------------------------------
# Journey 2 helpers, shared by the full (xfail) journey and the half that works.
# ---------------------------------------------------------------------------


def _open_invite_form(page, press):
    """Press the roster's "+ Invite an athlete" disclosure open.

    It's a `<details>` — on phone this needs a tap, not a click, to actually
    exercise touch input.
    """
    press(page.locator("summary", has_text="Invite an athlete"))


def _invite_email_message(to_email):
    """The coach-invite email addressed to ``to_email``, found by subject.

    Not by outbox position or recipient alone: once the invitee signs up,
    allauth's own signup-confirmation email lands in the same outbox,
    addressed to the same inbox.
    """
    matches = [
        m
        for m in mail.outbox
        if to_email in m.to and "invited you to train" in m.subject
    ]
    assert len(matches) == 1, (
        f"expected exactly one coach-invite email to {to_email}, found "
        f"{len(matches)} (outbox subjects: {[m.subject for m in mail.outbox]})"
    )
    return matches[0]


def _claim_url_from(message):
    """The absolute claim URL embedded in a coach-invite email's plain-text body."""
    match = re.search(r"https?://\S+/meso/claim/\S+/", message.body)
    assert match, f"no claim URL found in the invite email body:\n{message.body}"
    return match.group(0)


def _coach_sends_invite_and_gets_claim_url(page, press, shot, email):
    """Coach opens the roster, invites ``email``, and returns the emailed claim URL.

    Asserts the roster's own feedback (flash + Pending row) along the way, then
    reads the claim link back out of the email the view actually sent.
    """
    page.goto(reverse("meso:roster"))
    _open_invite_form(page, press)
    page.get_by_placeholder("athlete@email.com").fill(email)
    press(page.get_by_role("button", name="Send invite"))

    expect(page).to_have_url(re.compile(re.escape(reverse("meso:roster")) + r"$"))
    expect(page.get_by_text(f"Invite sent to {email}.")).to_be_visible()
    pending_row = page.locator(".meso-row").filter(has_text=email)
    expect(pending_row).to_be_visible()
    expect(pending_row.get_by_text("Pending")).to_be_visible()
    shot("01-invite-sent")

    return _claim_url_from(_invite_email_message(email))


def _invitee_follows_link_and_signs_up(invitee_page, press, shot, claim_url, email):
    """Brand-new invitee opens the claim link, bounces to login, and signs up.

    Doesn't assert where signup lands them afterward — that's exactly what the
    two journey-2 tests diverge on.
    """
    invitee_page.goto(claim_url)
    expect(invitee_page).to_have_url(re.compile(r"/accounts/login/\?next="))
    expect(
        invitee_page.get_by_role("heading", name="Login to your account")
    ).to_be_visible()
    shot("02-invitee-login-bounce", on=invitee_page)

    press(invitee_page.get_by_role("link", name="Sign up"))
    expect(
        invitee_page.get_by_role("heading", name="Create your account")
    ).to_be_visible()
    # A sitewide newsletter form in the page footer also has an "Email" field,
    # so scope to the signup form itself rather than `get_by_label`.
    signup_form = invitee_page.locator("#email-form")
    signup_form.locator("#id_email").fill(email)
    signup_form.locator("#id_password1").fill(INVITEE_PASSWORD)
    press(invitee_page.get_by_role("button", name="Sign Up"))

    invitee_page.wait_for_url(lambda url: "/accounts/signup/" not in url)
    shot("03-invitee-lands-after-signup", on=invitee_page)


# ---------------------------------------------------------------------------
# Journey 2: invite a brand-new athlete
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#523: the login page's Sign up link drops ?next, so a new invitee "
        "never gets back to the invite; #522: Accept on the claim page 403s "
        "(its no-referrer meta makes the browser send Origin: null)"
    ),
)
def test_new_athlete_signs_up_from_an_invite(
    page, viewport, shot, press, login, new_page, subscribed_coach
):
    """The whole journey: invite, sign up, accept, and show up on the roster.

    Two bugs stop it today. The login page's "Sign up" link is a bare
    `{% url 'account_signup' %}` that drops the bounce's `?next=`, so signup
    lands the invitee on `LOGIN_REDIRECT_URL` (`/users/profile/`) instead of
    back on the invite (#523). And pressing "Accept invite" 403s: the claim
    page's `no-referrer` meta makes the browser send `Origin: null` on its
    own form POST, which `CsrfViewMiddleware` rejects (#522). Strict, so the
    run goes red once both are fixed and the marker has to come off.
    """
    login(subscribed_coach.coach)
    claim_url = _coach_sends_invite_and_gets_claim_url(page, press, shot, INVITEE_EMAIL)

    invitee_page = new_page()
    _invitee_follows_link_and_signs_up(
        invitee_page, press, shot, claim_url, INVITEE_EMAIL
    )

    # Fail fast: `wait_for_url` above already resolved where signup sent them,
    # so this is a direct check, not another `expect()` waiting out a timeout.
    claim_path = urlparse(claim_url).path
    landed_path = urlparse(invitee_page.url).path
    assert landed_path == claim_path, (
        f"expected the invitee back on the claim page ({claim_path}), "
        f"landed on {invitee_page.url!r} instead"
    )
    expect(
        invitee_page.get_by_role("heading", name="Casey Coach invited you to train")
    ).to_be_visible()

    press(invitee_page.get_by_role("button", name="Accept invite"))
    expect(invitee_page).to_have_url(
        re.compile(re.escape(reverse("meso:athlete_home")) + r"$")
    )
    expect(
        invitee_page.get_by_text("You're now training with Casey Coach.")
    ).to_be_visible()
    shot("04-training-home", on=invitee_page)

    # A user with no name shows as their email's local part; the outstanding
    # invite row, which shows the whole address, is gone.
    page.goto(reverse("meso:roster"))
    expect(page.locator("a.meso-row").filter(has_text="jordan.new")).to_be_visible()
    expect(page.locator(".meso-row").filter(has_text=INVITEE_EMAIL)).to_have_count(0)
    shot("05-roster-active")


def test_new_person_signs_up_from_an_invite_link(
    page, viewport, shot, press, login, new_page, subscribed_coach
):
    """The half of the journey above that works today.

    The coach's invite form, the email and its link, the bounce to login and
    a real signup all work. Where signup lands is #523, so this doesn't look;
    it does what a real invitee does today and opens the emailed link again,
    and the invite is there for them. It stops before "Accept invite" (#522).
    Once both are fixed the xfail above covers all of this, and this test can
    go.
    """
    login(subscribed_coach.coach)
    claim_url = _coach_sends_invite_and_gets_claim_url(page, press, shot, INVITEE_EMAIL)

    invitee_page = new_page()
    _invitee_follows_link_and_signs_up(
        invitee_page, press, shot, claim_url, INVITEE_EMAIL
    )

    invitee_page.goto(claim_url)
    expect(
        invitee_page.get_by_role("heading", name="Casey Coach invited you to train")
    ).to_be_visible()
    expect(invitee_page.get_by_role("button", name="Accept invite")).to_be_visible()
    shot("04-invite-waiting", on=invitee_page)


# ---------------------------------------------------------------------------
# Journey 3: free coach at the athlete cap
# ---------------------------------------------------------------------------


def test_free_coach_at_the_cap_cannot_invite(
    page, viewport, shot, press, login, delivered_plan
):
    """A free coach with one active athlete (the cap) can't open a second invite."""
    coach = delivered_plan.coach
    login(coach)
    page.goto(reverse("meso:roster"))
    expect(page.get_by_text(re.compile(r"Free plan.*1 of 1 athlete"))).to_be_visible()
    expect(page.get_by_role("button", name="Subscribe")).to_be_visible()
    shot("01-roster-at-cap")

    second_email = "taylor.second@example.com"
    _open_invite_form(page, press)
    page.get_by_placeholder("athlete@email.com").fill(second_email)
    press(page.get_by_role("button", name="Send invite"))

    expect(page).to_have_url(re.compile(re.escape(reverse("meso:roster")) + r"$"))
    expect(page.get_by_text(SEAT_LIMIT_MESSAGE)).to_be_visible()
    expect(page.get_by_role("button", name="Subscribe")).to_be_visible()
    expect(page.locator(".meso-row").filter(has_text=second_email)).to_have_count(0)
    shot("02-seat-limit-message")

    assert not CoachInvite.objects.filter(email=second_email).exists()
    assert mail.outbox == []
