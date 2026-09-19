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

import pytest
from django.urls import reverse
from playwright.sync_api import expect

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
