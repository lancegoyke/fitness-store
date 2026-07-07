#!/usr/bin/env python3
"""Capture static product screenshots for the Meso landing page.

Issue #415, two modes in one script:

    uv run python scripts/capture_landing_still.py           # the hero shot
    uv run python scripts/capture_landing_still.py --cards    # the three
                                                                # how-it-works
                                                                # card stills

Both modes are siblings of ``scripts/record_demo.py`` (issue #388): rather
than duplicate its server-boot/seed/login/navigation plumbing, this script
imports it directly and drives the same real coach + athlete flow, taking
stills instead of finishing the storyboard and encoding a video. Same
deterministic data (``seed_demo_recording``), same env guardrails
(``MESO_AGENT_FAKE`` + blanked Anthropic/VAPID vars — see
``record_demo.SERVER_ENV_OVERRIDES``), same port (8035) and dev-server
lifecycle. The two modes never run at once, so sharing the port is fine.

Run via ``just capture-landing-still`` (hero) or ``just capture-landing-cards``
(the three card stills), with the dev Postgres/Redis containers already up
(``just services``) and the designer island built (``just frontend-build`` —
the designer page 404s on its JS/CSS bundle otherwise; both recipes do this
for you).

Hero mode writes ``app/store_project/static/webp/meso-landing-designer.webp``.

Cards mode drives one coach session (roster -> athlete profile -> designer ->
agent proposal -> review) and one athlete session (session view, phone
viewport) to write the three "how it works" card stills (the last section of
``meso/landing.html``):

    - meso-card-design.webp  — an *element* screenshot of the Designer's
      ``.meso-canvas`` pane only (no left rail, no agent chat) — a tighter
      crop than the hero shot, so the two don't read as the same image.
    - meso-card-deliver.webp — the athlete's session view, captured at a
      phone viewport (this card's pitch is "straight to the phone").
    - meso-card-adapt.webp   — the agent's review-changes screen, with the
      proposed diff list visible.

Refresh story (issue #415 "done when" — every visual here must be
regenerable, not a one-off binary): after a UI change to the Designer, the
athlete session view, or the review screen, re-run the matching recipe and
commit the new WebP(s). For the hero, deliver, and adapt shots (full-viewport
captures) nothing else needs updating — the landing template's
``width``/``height`` only encode an aspect ratio, and each always captures at
the same fixed viewport. The design card is the one exception: it's an
*element* crop of ``.meso-canvas``, whose box size follows the Designer's own
flex layout (left rail + chat panel widths) — if a future layout change
resizes that pane, re-running will produce a still with a different aspect
ratio, and ``landing.html``'s ``width``/``height`` for that one image would
need a matching bump.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright
from record_demo import AGENT_INSTRUCTION
from record_demo import BASE_URL
from record_demo import HIDE_DEBUG_TOOLBAR_CSS
from record_demo import LONG_BEAT
from record_demo import REPO_ROOT
from record_demo import build_env
from record_demo import log
from record_demo import offcamera_login
from record_demo import pause
from record_demo import resolve_demo_password
from record_demo import run_step
from record_demo import seed_demo_data
from record_demo import start_server
from record_demo import step_agent_proposal
from record_demo import step_athlete_profile
from record_demo import step_designer
from record_demo import step_roster
from record_demo import stop_server
from record_demo import wait_for_server

WEBP_DIR = REPO_ROOT / "app" / "store_project" / "static" / "webp"
RAW_DIR = REPO_ROOT / "docs" / "demo" / "out"

DEVICE_SCALE_FACTOR = 2

# ---------------------------------------------------------------------------
# Hero shot (unchanged since its original issue #415 step-1 landing)
# ---------------------------------------------------------------------------

OUT_PATH = WEBP_DIR / "meso-landing-designer.webp"
RAW_SCREENSHOT_PATH = RAW_DIR / "landing-designer-raw.png"

# The captured (and final, saved) pixel size — a 16:10 crop wide enough to show
# a populated week without the min-width:1240px designer root clipping.
# CAPTURE_VIEWPORT is rendered at DEVICE_SCALE_FACTOR (2x, i.e. "retina") and
# then downsampled by Pillow back to these same dimensions: the extra
# resolution buys crisper antialiasing (real supersampling), not a bigger
# on-disk image. The landing template's <img width/height> must match this
# ratio (1280:800 = 1.6).
CAPTURE_VIEWPORT = {"width": 1280, "height": 800}

WEBP_QUALITY = 82
MAX_BYTES = 250 * 1024

# ---------------------------------------------------------------------------
# How-it-works cards (issue #415 follow-up)
# ---------------------------------------------------------------------------

CARD_MAX_BYTES = 60 * 1024
CARD_WEBP_QUALITY = 82

# Design — same desktop viewport as the hero (same server/browser state,
# reached the same way), but the saved still is an *element* screenshot of
# just `.meso-canvas` (the grid pane, no left rail, no agent chat). Its box
# is 658x745 logical px at this viewport, given the Designer's own fixed
# rail (266px) + chat (356px) widths (designer-root.css / designer-rail.css /
# designer-chat.css) — see the module docstring's refresh-story note if that
# ever changes.
CARD_DESIGN_VIEWPORT = {"width": 1280, "height": 800}
CARD_DESIGN_TARGET = (658, 745)
CARD_DESIGN_OUT = WEBP_DIR / "meso-card-design.webp"
CARD_DESIGN_RAW = RAW_DIR / "landing-card-design-raw.png"

# Deliver — a phone-ish portrait viewport; the card's pitch is "straight to
# the phone", so the still should look like one.
CARD_DELIVER_VIEWPORT = {"width": 390, "height": 844}
CARD_DELIVER_TARGET = (390, 844)
CARD_DELIVER_OUT = WEBP_DIR / "meso-card-deliver.webp"
CARD_DELIVER_RAW = RAW_DIR / "landing-card-deliver-raw.png"

# Adapt — narrower than the hero/design viewport: the review page's content
# tops out at 860px (`.meso-page--narrow`), so a 1280-wide capture would be
# mostly margin either side of the diff list.
CARD_ADAPT_VIEWPORT = {"width": 1040, "height": 760}
CARD_ADAPT_TARGET = (1040, 760)
CARD_ADAPT_OUT = WEBP_DIR / "meso-card-adapt.webp"
CARD_ADAPT_RAW = RAW_DIR / "landing-card-adapt-raw.png"


# ---------------------------------------------------------------------------
# Shared WebP encoding
# ---------------------------------------------------------------------------


def _encode_webp(
    raw_path: Path,
    out_path: Path,
    target: tuple[int, int],
    *,
    quality: int,
    max_bytes: int,
) -> tuple[Path, int, int, int]:
    """Downsample a retina capture to ``target`` and encode it as WebP.

    Tries ``quality`` first; steps quality down if the result is still over
    ``max_bytes`` (a UI screenshot compresses far better than this in
    practice, but keep a margin rather than assume).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(raw_path) as im:
        im = im.convert("RGB")
        if im.size != target:
            im = im.resize(target, Image.LANCZOS)
        q = quality
        while True:
            im.save(out_path, "WEBP", quality=q, method=6)
            size_bytes = out_path.stat().st_size
            if size_bytes <= max_bytes or q <= 40:
                break
            q -= 10
        width, height = im.size
    raw_path.unlink(missing_ok=True)
    return out_path, width, height, size_bytes


# ---------------------------------------------------------------------------
# Hero capture
# ---------------------------------------------------------------------------


def capture_screenshot(env) -> Path:
    RAW_SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    password = resolve_demo_password()
    coach_email, _athlete_email = run_step(
        "SEED DEMO DATA", seed_demo_data, env, password
    )
    server_proc = start_server(env)
    try:
        run_step(
            "WAIT FOR DEV SERVER",
            wait_for_server,
            server_proc,
            f"{BASE_URL}/accounts/login/",
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                storage_state = run_step(
                    "OFF-CAMERA LOGIN", offcamera_login, browser, coach_email, password
                )
                context = browser.new_context(
                    storage_state=storage_state,
                    viewport=CAPTURE_VIEWPORT,
                    device_scale_factor=DEVICE_SCALE_FACTOR,
                )
                context.add_init_script(HIDE_DEBUG_TOOLBAR_CSS)
                page = context.new_page()

                run_step("STEP 1 ROSTER", step_roster, page)
                run_step("STEP 2 ATHLETE PROFILE", step_athlete_profile, page)
                run_step("STEP 3 DESIGNER", step_designer, page)

                run_step(
                    "SCREENSHOT",
                    page.screenshot,
                    path=str(RAW_SCREENSHOT_PATH),
                )
                context.close()
            finally:
                browser.close()
    finally:
        stop_server(server_proc)
    return RAW_SCREENSHOT_PATH


def optimize(raw_path: Path) -> tuple[Path, int, int, int]:
    target = (CAPTURE_VIEWPORT["width"], CAPTURE_VIEWPORT["height"])
    return _encode_webp(
        raw_path, OUT_PATH, target, quality=WEBP_QUALITY, max_bytes=MAX_BYTES
    )


# ---------------------------------------------------------------------------
# Card captures
# ---------------------------------------------------------------------------


def _capture_design_and_adapt(browser, coach_email, password):
    """Design card (``.meso-canvas`` element crop) + Adapt card (review page).

    One continuous coach session/context — reaching the review screen is
    just the Designer flow's next step (type an instruction, follow the
    resulting review link), so there's no reason to pay for a second
    off-camera login.
    """
    storage_state = offcamera_login(browser, coach_email, password)
    context = browser.new_context(
        storage_state=storage_state,
        viewport=CARD_DESIGN_VIEWPORT,
        device_scale_factor=DEVICE_SCALE_FACTOR,
    )
    context.add_init_script(HIDE_DEBUG_TOOLBAR_CSS)
    page = context.new_page()
    try:
        step_roster(page)
        step_athlete_profile(page)
        step_designer(page)

        canvas = page.locator(".meso-canvas")
        expect(canvas).to_be_visible()
        canvas.screenshot(path=str(CARD_DESIGN_RAW))

        step_agent_proposal(page, AGENT_INSTRUCTION)
        apply_button = page.get_by_test_id("review-apply")
        expect(apply_button).to_be_visible()
        expect(apply_button).to_be_enabled()

        # Narrower than the Designer viewport (the review page's content
        # tops out at 860px) — resize in place rather than paying for a
        # third context/login.
        page.set_viewport_size(CARD_ADAPT_VIEWPORT)
        pause(page, LONG_BEAT)
        page.screenshot(path=str(CARD_ADAPT_RAW))
    finally:
        context.close()


def _capture_deliver(browser, athlete_email, password):
    """Deliver card — Maya's session view, at a phone viewport."""
    storage_state = offcamera_login(browser, athlete_email, password)
    context = browser.new_context(
        storage_state=storage_state,
        viewport=CARD_DELIVER_VIEWPORT,
        device_scale_factor=DEVICE_SCALE_FACTOR,
    )
    context.add_init_script(HIDE_DEBUG_TOOLBAR_CSS)
    page = context.new_page()
    try:
        page.goto(f"{BASE_URL}/meso/me/")
        # An already-logged day (not "To do") — filled reps/load/rpe and
        # checked-off sets read as a real, in-progress program rather than an
        # empty grid.
        logged_row = page.locator("a.meso-row", has_text="Logged").first
        expect(logged_row).to_be_visible()
        pause(page)
        with page.expect_navigation(url=re.compile(r"/meso/me/session/")):
            logged_row.click()
        expect(page.get_by_test_id("set-toggle").first).to_be_visible()
        pause(page)
        page.screenshot(path=str(CARD_DELIVER_RAW))
    finally:
        context.close()


def capture_cards(env) -> dict[str, Path]:
    for raw in (CARD_DESIGN_RAW, CARD_DELIVER_RAW, CARD_ADAPT_RAW):
        raw.parent.mkdir(parents=True, exist_ok=True)
    password = resolve_demo_password()
    coach_email, athlete_email = run_step(
        "SEED DEMO DATA", seed_demo_data, env, password
    )
    server_proc = start_server(env)
    try:
        run_step(
            "WAIT FOR DEV SERVER",
            wait_for_server,
            server_proc,
            f"{BASE_URL}/accounts/login/",
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                run_step(
                    "CARDS: DESIGN + ADAPT (coach)",
                    _capture_design_and_adapt,
                    browser,
                    coach_email,
                    password,
                )
                run_step(
                    "CARD: DELIVER (athlete)",
                    _capture_deliver,
                    browser,
                    athlete_email,
                    password,
                )
            finally:
                browser.close()
    finally:
        stop_server(server_proc)
    return {
        "design": CARD_DESIGN_RAW,
        "deliver": CARD_DELIVER_RAW,
        "adapt": CARD_ADAPT_RAW,
    }


def optimize_cards(raw_paths: dict[str, Path]) -> dict[str, tuple[Path, int, int, int]]:
    return {
        "design": _encode_webp(
            raw_paths["design"],
            CARD_DESIGN_OUT,
            CARD_DESIGN_TARGET,
            quality=CARD_WEBP_QUALITY,
            max_bytes=CARD_MAX_BYTES,
        ),
        "deliver": _encode_webp(
            raw_paths["deliver"],
            CARD_DELIVER_OUT,
            CARD_DELIVER_TARGET,
            quality=CARD_WEBP_QUALITY,
            max_bytes=CARD_MAX_BYTES,
        ),
        "adapt": _encode_webp(
            raw_paths["adapt"],
            CARD_ADAPT_OUT,
            CARD_ADAPT_TARGET,
            quality=CARD_WEBP_QUALITY,
            max_bytes=CARD_MAX_BYTES,
        ),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    env = build_env()

    if "--cards" in sys.argv[1:]:
        raw_paths = capture_cards(env)
        results = run_step("OPTIMIZE CARDS", optimize_cards, raw_paths)
        for out_path, width, height, size_bytes in results.values():
            log(
                f"\nwrote {out_path.relative_to(REPO_ROOT)} "
                f"({width}x{height}, {size_bytes / 1024:.1f} KB)"
            )
        return

    raw_path = capture_screenshot(env)
    out_path, width, height, size_bytes = run_step("OPTIMIZE", optimize, raw_path)
    log(
        f"\nwrote {out_path.relative_to(REPO_ROOT)} "
        f"({width}x{height}, {size_bytes / 1024:.1f} KB)"
    )


if __name__ == "__main__":
    main()
