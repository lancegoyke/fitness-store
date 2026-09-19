"""Browser check for #555 P1-C — billing dates render in the viewer's local timezone.

``meso_local_dates.js`` rewrites the server's UTC date on load; this drives a
real Chromium context pinned to America/Los_Angeles and reads the visible
text after the rewrite runs.
"""

from datetime import timedelta

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone as django_timezone
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.models import CoachSubscription
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_billing_page_shows_the_la_local_date(new_context, live_server):
    """A trial_end at 01:00 UTC is the previous day in Los Angeles (UTC-7/-8)."""
    coach = UserFactory(name="Tz Coach", email="tz.coach@example.com")
    CoachProfileFactory(user=coach)
    # 01:00 UTC at least three days out: past the 48h Checkout minimum, and
    # always the previous calendar day in Los Angeles.
    trial_end = (django_timezone.now() + timedelta(days=3)).replace(
        hour=1, minute=0, second=0, microsecond=0
    )
    la_date = trial_end - timedelta(days=1)
    expected = f"{la_date:%b} {la_date.day}"
    CoachSubscriptionFactory(
        coach=coach, status=CoachSubscription.Status.TRIALING, trial_end=trial_end
    )

    client = Client()
    client.force_login(coach)
    session_cookie = client.cookies[settings.SESSION_COOKIE_NAME]

    context = new_context(
        viewport={"width": 1280, "height": 720},
        timezone_id="America/Los_Angeles",
    )
    context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": session_cookie.value,
                "url": live_server.url,
            }
        ]
    )
    page = context.new_page()
    page.goto(f"{live_server.url}/meso/billing/")

    times = page.locator("time[data-local-date]")
    visible = [times.nth(i).inner_text() for i in range(times.count())]
    assert visible
    assert all(text == expected for text in visible), (visible, expected)
