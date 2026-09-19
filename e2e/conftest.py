"""Shared fixtures for the local, opt-in headless-browser E2E suite (#506).

Only collected when `e2e/` is explicitly targeted — `testpaths = ["app"]` in
pyproject.toml keeps a plain `uv run pytest` out of this directory entirely,
so nothing here (including the env var below) affects the default suite. Run
via `just e2e` (see the justfile), not directly, unless you know what you're
doing.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import ProposedChange
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.meso.tests._helpers import sub_line
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
# Viewports — every journey runs at all three (issue #506's desktop + phone,
# plus #508's narrower 360px phone).
# ---------------------------------------------------------------------------


@pytest.fixture(params=["desktop", "phone", "phone-360"])
def viewport(request, playwright):
    """One of the sizes every journey runs at, as context-args + a flag.

    `is_phone` drives the `press()` helper (tap vs click) and lets a test
    branch on how a real user would move focus (e.g. Tab on desktop, tapping
    the next field on phone). Both phone sizes set it; `just e2e -k phone`
    runs the two of them.

    `phone-360` (issue #508) is the narrow Android class (Galaxy S8 width,
    taller than that preset to match a current handset): a layout that only
    fits at 390 overflows here first.
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
    elif request.param == "phone-360":
        android_user_agent = playwright.devices["Galaxy S8"]["user_agent"]
        context_args = {
            "viewport": {"width": 360, "height": 780},
            "is_mobile": True,
            "has_touch": True,
            "device_scale_factor": 3,
            "user_agent": android_user_agent,
        }
    else:
        context_args = {"viewport": {"width": 1280, "height": 720}}
    return {
        "id": request.param,
        "is_phone": request.param != "desktop",
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


@pytest.fixture
def block_plan(db):
    """A three-week block with a four-line prescription (issue #508).

    What the athlete's phone layout needs that `delivered_plan` lacks: several
    weeks (so the week chips have somewhere to go) and a cell whose sub-lines
    stack four deep (so the stacked view has lines to keep apart). Two days,
    so the stacked view shows more than one day card.

    Back Squat is a %1RM lift (its text carries "NN%"), so the session page
    also renders the 1RM input. Each week's squat prescription is different,
    so switching weeks visibly changes what the stacked view says.
    """
    coach = UserFactory(name="Casey Coach", email="casey.coach@example.com")
    athlete = UserFactory(name="Alex Athlete", email="alex.athlete@example.com")
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(
        relationship=rel, title="Strength Block", status=Plan.Status.ACTIVE
    )
    mesocycle = MesocycleFactory(plan=plan, name="Accumulation", order=0)
    weeks = [
        WeekFactory(mesocycle=mesocycle, index=1, delivered_at=timezone.now()),
        WeekFactory(mesocycle=mesocycle, index=2),
        WeekFactory(mesocycle=mesocycle, index=3, is_deload=True),
    ]
    squat_lines = {
        1: ["4 x 6 @ 70%", "RPE 7", "Rest 2-3 min", "Brace before every rep"],
        2: ["4 x 5 @ 75%", "RPE 8", "Rest 3 min", "Pause the first rep"],
        3: ["3 x 5 @ 60%", "RPE 6", "Rest 2 min", "Move fast, stay crisp"],
    }
    lower = upper = None
    squat_slot = rdl_slot = bench_slot = row_slot = None
    lower_sessions = {}
    for week in weeks:
        n = week.index
        lower = day(
            week,
            day_number=1,
            name="Lower",
            bias="Squat",
            session_slot=lower.session_slot if lower else None,
        )
        upper = day(
            week,
            day_number=2,
            name="Upper",
            bias="Press",
            session_slot=upper.session_slot if upper else None,
        )
        lower_sessions[n] = lower
        first, *rest = squat_lines[n]
        squat = presc(
            lower, name="Back Squat", order=0, exercise_slot=squat_slot, text=first
        )
        squat_slot = squat.exercise_slot
        for text in rest:
            sub_line(squat, text)
        rdl = presc(
            lower,
            name="Romanian Deadlift",
            order=1,
            exercise_slot=rdl_slot,
            text=f"3 x {10 - n}, RPE 8",
        )
        rdl_slot = rdl.exercise_slot
        bench = presc(
            upper,
            name="Bench Press",
            order=0,
            exercise_slot=bench_slot,
            text=f"4 x {7 - n}, RPE 8",
        )
        bench_slot = bench.exercise_slot
        row = presc(
            upper,
            name="Chest-Supported Row",
            order=1,
            exercise_slot=row_slot,
            text="3 x 10-12",
        )
        row_slot = row.exercise_slot
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        plan=plan,
        weeks=weeks,
        squat_lines=squat_lines,
        lower_sessions=lower_sessions,
    )


@pytest.fixture
def logged_plan(delivered_plan):
    """`delivered_plan` with the athlete's Box Squat logged as "1×5 @ 100 kg".

    Logged through the exact endpoints the athlete's own UI calls (the cell
    write, then "Log session"), not by writing rows directly. Shared by the
    coach-results journey (#506) and `coach_workspace` below (#508).
    """
    client = Client()
    client.force_login(delivered_plan.athlete)
    cell_response = client.post(
        reverse("meso:athlete_cell_write", kwargs={"pk": delivered_plan.session.pk}),
        data=json.dumps(
            {"exercise_id": delivered_plan.squat.pk, "line": 1, "text": "100 x 5"}
        ),
        content_type="application/json",
    )
    assert cell_response.status_code == 200
    log_response = client.post(
        reverse("meso:athlete_log_session", kwargs={"pk": delivered_plan.session.pk}),
        data=json.dumps({"status": "done", "sets": []}),
        content_type="application/json",
    )
    assert log_response.status_code == 200
    return delivered_plan


@pytest.fixture
def coach_workspace(logged_plan):
    """`logged_plan` plus everything else the coach-mobile suite needs (#508).

    Adds a PENDING agent review batch on the plan with two
    proposed changes — one carrying `honors` — and a template plan owned by
    the coach (so the template library has a row to lay out).
    """
    batch = AgentProposalBatchFactory(
        plan=logged_plan.plan, status=AgentProposalBatch.Status.PENDING
    )
    ProposedChangeFactory(
        batch=batch,
        kind=ProposedChange.Kind.PROGRESS,
        day_label="Day 1 · Lower",
        title="Progress Box Squat",
        before="3 x 6 @ 70 kg",
        after="3 x 6 @ 72.5 kg",
        rationale="RPE has come in under target the last two sessions.",
        honors="",
    )
    change_with_honors = ProposedChangeFactory(
        batch=batch,
        kind=ProposedChange.Kind.DELOAD,
        day_label="Day 1 · Lower",
        title="Deload the RDL",
        before="3 x 8 @ 80 kg",
        after="3 x 8 @ 65 kg",
        rationale="Honoring the athlete's note about lower-back fatigue.",
        honors="lower-back fatigue note",
    )

    template = PlanFactory(
        relationship=None,
        is_template=True,
        owner=logged_plan.coach,
        title="Push/Pull/Legs Template",
    )
    template_mesocycle = MesocycleFactory(plan=template, name="Block 1", order=0)
    template_week = WeekFactory(mesocycle=template_mesocycle, index=1)
    template_session = day(template_week, day_number=1, name="Push")
    presc(template_session, name="Bench Press", sets="4", reps="6", load="80", rpe="7")

    return SimpleNamespace(
        **vars(logged_plan),
        batch=batch,
        change_with_honors=change_with_honors,
        template=template,
    )
