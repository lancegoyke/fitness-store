"""Athlete home: the push opt-in card shows only when it should (issue #517).

`meso_push.js`'s `refreshCta()` reveals `#meso-push-cta` — "Get notified when
your coach delivers" — only when push is usable AND the browser hasn't
decided yet (`Notification.permission === "default"`). Everywhere else
(unsupported, already granted, already denied) it should stay hidden, the way
the markup ships it (`hidden` on the `<button>`). The bug: that same button
also carries an inline `style="display:flex;…"`, and an author's inline
`display` always beats the UA's `[hidden] { display: none }` — so the card
showed on every load no matter what the JS decided.

Each case stubs the world with `context.add_init_script(...)`, which runs
before any of the page's own scripts, so by the time `meso_push.js` (a
`defer`red script) reads `Notification.permission` / `"PushManager" in
window` on load, it sees the stubbed values. Case 4 below (supported +
undecided → card SHOWN) is the guard against a "fix" that just hides the
card for everyone — it must keep passing.
"""

import re

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.notifications.models import PushKind
from store_project.notifications.models import PushNotification

pytestmark = pytest.mark.django_db

# A real-looking base64url VAPID public key (same shape as
# config/settings/test.py's), so `urlBase64ToUint8Array` wouldn't choke if a
# case ever reached it.
VAPID_PUBLIC_KEY = (
    "BGHM4CGuxntiwQWPBTFdfjMsWpqiIjDLriWlfxSCk-_D"
    "iAcJ0ttNeSR3CJNr0GcktI3le-JgEb7ydvDoQEpUmd0"
)
VAPID_PRIVATE_KEY = (
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgIKQKjrxm3qC3Ja7C2XVf"
    "vzGySCvOe4gwCL9bJhcKlZmhRANCAARhzOAhrsZ7YsEFjwUxXX4zLFqaoiIwy64lpX8U"
    "gpPvw4gHCdLbTXkkdwiTa9BnJLSN5XviYBG-8nbw6EBKVJnd"
)

NO_PUSH_MANAGER_JS = "delete window.PushManager;"


def _permission_stub(value):
    """The browser already decided, before the page's own scripts run."""
    return (
        "Object.defineProperty(Notification, 'permission', "
        f"{{ get: () => '{value}', configurable: true }});"
    )


# The card is answered by tapping it, starting from `default`.
# `Notification.requestPermission()` resolves `denied`, not `granted`: on a
# grant, `enable()` awaits `subscribe()`, which awaits
# `navigator.serviceWorker.ready` — that promise never resolves because this
# suite's contexts block service workers (`conftest.py`'s
# `service_workers="block"`), so `refreshCta()` would never run and the test
# could never observe the card hide.
ANSWER_DENIED_JS = """
(() => {
  let perm = "default";
  Object.defineProperty(Notification, "permission", {
    get: () => perm,
    configurable: true,
  });
  Notification.requestPermission = async () => {
    perm = "denied";
    return "denied";
  };
})();
"""


def _enable_push(settings):
    settings.MESO_VAPID_PUBLIC_KEY = VAPID_PUBLIC_KEY
    settings.MESO_VAPID_PRIVATE_KEY = VAPID_PRIVATE_KEY


def _cta(page):
    return page.locator("#meso-push-cta")


def _goto_athlete_home(page, login, delivered_plan):
    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_home"))
    expect(page.get_by_role("heading", name="Your programs")).to_be_visible()


@pytest.mark.parametrize(
    "case,init_script",
    [
        ("no-pushmanager", NO_PUSH_MANAGER_JS),
        ("already-granted", _permission_stub("granted")),
        ("already-denied", _permission_stub("denied")),
    ],
    ids=["no-pushmanager", "already-granted", "already-denied"],
)
def test_push_cta_stays_hidden_when_not_offerable(
    page, context, viewport, shot, login, delivered_plan, settings, case, init_script
):
    _enable_push(settings)
    context.add_init_script(init_script)
    _goto_athlete_home(page, login, delivered_plan)

    expect(_cta(page)).to_be_hidden()
    if case == "no-pushmanager":
        shot("01-cta-hidden")


def test_push_cta_shows_when_supported_and_undecided(
    page, context, viewport, shot, login, delivered_plan, settings
):
    _enable_push(settings)
    context.add_init_script(_permission_stub("default"))
    _goto_athlete_home(page, login, delivered_plan)

    cta = _cta(page)
    expect(cta).to_be_visible()
    expect(cta.get_by_text("Enable")).to_be_visible()
    shot("01-cta-visible")


def test_push_cta_hides_after_the_athlete_answers_from_the_card(
    page, context, viewport, shot, press, login, delivered_plan, settings
):
    _enable_push(settings)
    context.add_init_script(ANSWER_DENIED_JS)
    _goto_athlete_home(page, login, delivered_plan)

    cta = _cta(page)
    expect(cta).to_be_visible()

    press(cta)

    expect(cta).to_be_hidden()


@pytest.mark.parametrize("viewport", ["phone"], indirect=True)
def test_push_permission_answer_reaches_the_server(
    page, context, viewport, shot, press, login, delivered_plan, settings
):
    """Tapping the CTA and answering "denied" lands a `push_permission` event.

    Phone only, the same reasoning `test_meso_coach_agent.py` gives for
    pinning `desktop` on the designer/review screens — this CTA is an
    athlete-phone affordance, so there's no reason to drive it three times.

    Reuses `ANSWER_DENIED_JS` (see its own comment above): "denied" is the
    only answer this suite's blocked service workers let a test ever
    observe, because a "granted" answer would make `enable()` await
    `subscribe()`, which awaits `navigator.serviceWorker.ready` — a promise
    this suite's `service_workers="block"` context never resolves. But this
    test asks a different question than the CTA-visibility tests above: not
    "does the card hide after the athlete answers" but "does the server
    actually persist what the athlete tapped".

    `meso_push.js` reports the answer with a fire-and-forget `fetch()` to
    the beacon — nothing on the page awaits it or reflects it in the DOM, so
    there is nothing to assert against in the browser and nothing to poll or
    sleep for either. Waiting on the beacon's own network response with
    `page.expect_response()` is the one deterministic signal that the POST
    has actually landed; because this suite runs against a real
    `live_server` backed by a real database (not a test transaction wrapped
    around the whole test), the row `track_beacon` wrote is already visible
    to this process's own queries the moment that response resolves — no
    extra wait needed on top of it.
    """
    _enable_push(settings)
    context.add_init_script(ANSWER_DENIED_JS)
    _goto_athlete_home(page, login, delivered_plan)

    cta = _cta(page)
    expect(cta).to_be_visible()

    with page.expect_response(
        lambda r: "/meso/api/track/" in r.url and r.status == 204
    ):
        press(cta)

    shot("01-denied")

    event = Event.objects.get(
        name=EventName.PUSH_PERMISSION, actor=delivered_plan.athlete
    )
    assert event.props["result"] == "denied"
    assert event.source == Event.Source.CLIENT


def test_push_click_lands_and_the_query_param_disappears(
    page, viewport, shot, login, delivered_plan
):
    """A `?n=<id>` deep link marks the push clicked once and cleans its own URL.

    All three viewports (the default `viewport` fixture, no override): unlike
    the CTA test above, this is a plain server-rendered navigation — nothing
    here depends on touch vs. mouse or on screen width, so there's no reason
    to pin one size the way the CTA test does.

    Builds the `PushNotification` row directly, the same shape
    `notifications.push.log_push_sent` would have opened for a real send,
    rather than actually sending a push through a mocked `pywebpush` — the
    thing under test is what happens when the athlete's browser LANDS on the
    `?n=` URL, not how the notification got sent in the first place.

    `meso.views.AthleteHomeView.get_context_data` calls `record_push_click()`
    (which flips `clicked_at` and fires `push_clicked`) before the template
    even renders, so the click is already recorded by the time the response
    body reaches the browser. But `_pwa_head.html`'s `?n=` strip runs as part
    of the page's own inline script, after the DOM is parsed — so the address
    bar itself needs a real wait (`expect(page).to_have_url(...)`, which
    retries) rather than a same-tick `page.url` read that could still catch
    the pre-strip URL.

    A second load of the now-bare URL proves the click is truly one-shot: the
    server's guard is an `UPDATE ... WHERE clicked_at IS NULL`, so a reload
    (or a bookmark, or the link shared onward) must not move `clicked_at`
    again or add a second `push_clicked` event.
    """
    notification = PushNotification.objects.create(
        kind=PushKind.BLOCK_DELIVERED, user=delivered_plan.athlete
    )
    login(delivered_plan.athlete)

    home_url = reverse("meso:athlete_home")
    page.goto(f"{home_url}?n={notification.pk}")
    expect(page.get_by_role("heading", name="Your programs")).to_be_visible()
    expect(page).to_have_url(re.compile(re.escape(home_url) + r"$"))
    shot("01-clicked")

    notification.refresh_from_db()
    assert notification.clicked_at is not None
    first_clicked_at = notification.clicked_at

    def _push_clicked_events():
        return Event.objects.filter(
            name=EventName.PUSH_CLICKED, actor=delivered_plan.athlete
        )

    assert _push_clicked_events().count() == 1

    # Reload the now-stripped URL — the click must not count a second time.
    page.reload()
    expect(page.get_by_role("heading", name="Your programs")).to_be_visible()

    notification.refresh_from_db()
    assert notification.clicked_at == first_clicked_at
    assert _push_clicked_events().count() == 1
