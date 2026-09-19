"""Shared fixtures for the local, opt-in headless-browser E2E suite (#506).

Only collected when `e2e/` is explicitly targeted — `testpaths = ["app"]` in
pyproject.toml keeps a plain `uv run pytest` out of this directory entirely,
so nothing here (including the env var below) affects the default suite. Run
via `just e2e` (see the justfile), not directly, unless you know what you're
doing.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.utils import timezone
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.users.factories import UserFactory

# pytest-playwright's sync API keeps an asyncio event loop running on the test
# thread, so Django's async-safety check refuses ORM calls made from test code
# (fixtures, test bodies) with SynchronousOnlyOperation. Django reads this flag
# at call time, so setting it here is enough, and because this conftest only
# loads when pytest collects `e2e/`, the default suite never sees it.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

E2E_DIR = Path(__file__).parent

# Screenshots are judged by eye, not asserted on — keep them out of
# pytest-playwright's own `--output` dir, which the plugin WIPES at the start
# of every session (`delete_output_dir`); this folder is only ever appended
# to (`shot()` below overwrites individual files, never the directory).
SCREENSHOT_DIR = E2E_DIR / "screenshots"


def pytest_collection_modifyitems(config, items):
    """Mark every item collected from this directory `e2e`.

    This hook sees the whole session's items, not just this directory's, so a
    run that collects `app/` and `e2e/` together must only mark the ones here.
    """
    for item in items:
        if item.path.is_relative_to(E2E_DIR):
            item.add_marker(pytest.mark.e2e)


# ---------------------------------------------------------------------------
# Viewports — every journey runs at both (issue #506's "two viewports").
# ---------------------------------------------------------------------------


@pytest.fixture(params=["desktop", "phone"])
def viewport(request, playwright):
    """One of the two sizes every journey runs at, as context-args + a flag.

    `is_phone` drives the `press()` helper (tap vs click) and lets a test
    branch on how a real user would move focus (e.g. Tab on desktop, tapping
    the next field on phone).
    """
    if request.param == "phone":
        # Only the user agent comes from the iPhone 13 device preset — the
        # rest is spelled out explicitly per the design brief, so a future
        # Playwright device-preset change can't quietly redefine "phone".
        iphone_user_agent = playwright.devices["iPhone 13"]["user_agent"]
        context_args = {
            "viewport": {"width": 390, "height": 844},
            "is_mobile": True,
            "has_touch": True,
            "device_scale_factor": 3,
            "user_agent": iphone_user_agent,
        }
    else:
        context_args = {"viewport": {"width": 1280, "height": 720}}
    return {
        "id": request.param,
        "is_phone": request.param == "phone",
        "context_args": context_args,
    }


@pytest.fixture
def browser_context_args(browser_context_args, live_server, viewport):
    """Extend pytest-playwright's own fixture, not replace it.

    `base_url` lets tests `page.goto("/meso/me/")` against the in-process
    `live_server`; `service_workers="block"` keeps the athlete PWA's service
    worker from ever caching a response — a caching bug could otherwise hide
    a real server change behind a stale cache, which is exactly what this
    suite exists to catch.
    """
    return {
        **browser_context_args,
        "base_url": live_server.url,
        "service_workers": "block",
        **viewport["context_args"],
    }


# ---------------------------------------------------------------------------
# Interaction helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def login(context, live_server):
    """`login(user)` — cookie-login a Playwright context as `user`.

    Logs a Django test `Client` in with `force_login` (no real form submit)
    and copies its session cookie into the browser context, so a test can
    open straight on the page under test. `test_login.py` is the one
    exception that drives the real allauth form end to end; every other
    journey uses this. Returns the `Client`, in case a test also wants to
    make authenticated requests outside the browser (the coach-results
    journey does, to produce the athlete's log via the same endpoints the
    athlete UI calls).
    """

    def _login(user):
        client = Client()
        client.force_login(user)
        session_cookie = client.cookies[settings.SESSION_COOKIE_NAME]
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": session_cookie.value,
                    "url": live_server.url,
                }
            ]
        )
        return client

    return _login


@pytest.fixture
def press(viewport):
    """`press(locator)` — tap on phone, click on desktop.

    So a phone journey actually exercises touch input rather than a mouse
    click Chromium happens to accept anyway.
    """

    def _press(locator):
        if viewport["is_phone"]:
            locator.tap()
        else:
            locator.click()

    return _press


@pytest.fixture
def shot(page, viewport, request):
    """`shot(step)` — a full-page screenshot for `<journey>/<step>--<viewport>.png`.

    `journey` is the test function's own name (parametrize suffixes like
    `[desktop]` stripped), with a leading `test_` dropped — so
    `test_athlete_logs_a_typed_set[phone]` writes into
    `athlete_logs_a_typed_set/`. Overwrites a given step+viewport on re-run;
    never wipes the folder (unlike pytest-playwright's own `--output` dir).

    It scrolls to the top first. A full-page capture of a scrolled page paints
    the sticky Meso topnav wherever the viewport was, halfway down the image.
    """
    name = getattr(request.node, "originalname", None) or request.node.name
    if name.startswith("test_"):
        name = name[len("test_") :]
    folder = SCREENSHOT_DIR / name

    def _shot(step):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{step}--{viewport['id']}.png"
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(path), full_page=True)
        return path

    return _shot


# ---------------------------------------------------------------------------
# Isolation — mirrors app/store_project/conftest.py, which doesn't reach here
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the process-wide LocMemCache before every test.

    `SESSION_ENGINE` is the cache backend (config/settings/base.py:426), and
    `CACHES["default"]` is a plain `LocMemCache` shared by every thread in
    this process — including the `live_server` thread — which is exactly
    what lets a `login()` fixture's cookie-login be visible to it. But that
    same process-wide cache is NOT reset between tests the way
    `transactional_db` resets the database, so a stale session (or anything
    else a previous test cached) could otherwise leak forward.
    """
    cache.clear()


@pytest.fixture(autouse=True)
def _media_storage(settings, tmp_path):
    """As `app/store_project/conftest.py`'s `media_storage` — scoped here too.

    That fixture's `autouse` only reaches tests under `app/store_project/`;
    `e2e/` is a sibling directory, so it needs its own copy.
    """
    settings.MEDIA_ROOT = str(tmp_path)


# ---------------------------------------------------------------------------
# Data — a delivered plan real enough to log a set against.
# ---------------------------------------------------------------------------


@pytest.fixture
def delivered_plan(db):
    """Coach + athlete + one delivered week with two prescribed lifts.

    The same shape as `store_project.meso.tests.test_parse_at_commit.seed()`
    (coach/athlete → active `CoachAthlete` → active `Plan` → `Mesocycle` →
    delivered `Week` → "Lower" `Session` → Box Squat + RDL), rebuilt here
    (rather than imported — that module lives under `app/`, outside this
    suite's collection root) with explicit, readable names so a screenshot
    reads like a real coach/athlete pair instead of Faker noise.
    """
    coach = UserFactory(name="Casey Coach", email="casey.coach@example.com")
    athlete = UserFactory(name="Alex Athlete", email="alex.athlete@example.com")
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    mesocycle = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=mesocycle, index=1, delivered_at=timezone.now())
    session = day(week, day_number=1, name="Lower", bias="Quad")
    squat = presc(
        session, name="Box Squat", order=0, sets="3", reps="6", load="70", rpe="7"
    )
    rdl = presc(session, name="RDL", order=1, sets="3", reps="8", load="80", rpe="8")
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        rel=rel,
        plan=plan,
        mesocycle=mesocycle,
        week=week,
        session=session,
        squat=squat,
        rdl=rdl,
    )
