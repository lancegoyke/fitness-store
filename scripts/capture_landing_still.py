#!/usr/bin/env python3
"""Capture a static hero screenshot of the Designer for the Meso landing page.

Issue #415, step 1 only — the hosted walkthrough video and per-card
screenshots named in that issue are explicit follow-ups, not this script's job.

Sibling to ``scripts/record_demo.py`` (issue #388): rather than duplicate its
server-boot/seed/login/navigation plumbing, this script imports it directly
and drives the same real coach flow through STEP 1 ROSTER → STEP 2 ATHLETE
PROFILE → STEP 3 DESIGNER, then takes one screenshot instead of finishing the
storyboard and encoding a video. Same deterministic data
(``seed_demo_recording``), same env guardrails (``MESO_AGENT_FAKE`` +
blanked Anthropic/VAPID vars — see ``record_demo.SERVER_ENV_OVERRIDES``), same
port (8035) and dev-server lifecycle. The two scripts never run at once, so
sharing the port is fine.

Run via ``just capture-landing-still`` (``uv run python
scripts/capture_landing_still.py``), with the dev Postgres/Redis containers
already up (``just services``) and the designer island built (``just
frontend-build`` — the designer page 404s on its JS/CSS bundle otherwise).
Writes ``app/store_project/static/webp/meso-landing-designer.webp``.

Refresh story (issue #415 "done when" — the hero visual must be regenerable,
not a one-off binary): after a Designer UI change, re-run this recipe and
commit the new WebP. Nothing else to update — the landing template's
``width``/``height`` attributes only encode an aspect ratio, and this script
always captures at the same viewport.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright
from record_demo import BASE_URL
from record_demo import HIDE_DEBUG_TOOLBAR_CSS
from record_demo import REPO_ROOT
from record_demo import build_env
from record_demo import log
from record_demo import offcamera_login
from record_demo import resolve_demo_password
from record_demo import run_step
from record_demo import seed_demo_data
from record_demo import start_server
from record_demo import step_athlete_profile
from record_demo import step_designer
from record_demo import step_roster
from record_demo import stop_server
from record_demo import wait_for_server

OUT_PATH = (
    REPO_ROOT
    / "app"
    / "store_project"
    / "static"
    / "webp"
    / "meso-landing-designer.webp"
)
RAW_SCREENSHOT_PATH = REPO_ROOT / "docs" / "demo" / "out" / "landing-designer-raw.png"

# The captured (and final, saved) pixel size — a 16:10 crop wide enough to show
# a populated week without the min-width:1240px designer root clipping.
# CAPTURE_VIEWPORT is rendered at DEVICE_SCALE_FACTOR (2x, i.e. "retina") and
# then downsampled by Pillow back to these same dimensions: the extra
# resolution buys crisper antialiasing (real supersampling), not a bigger
# on-disk image. The landing template's <img width/height> must match this
# ratio (1280:800 = 1.6).
CAPTURE_VIEWPORT = {"width": 1280, "height": 800}
DEVICE_SCALE_FACTOR = 2

WEBP_QUALITY = 82
MAX_BYTES = 250 * 1024


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
    """Downsample the retina capture to CAPTURE_VIEWPORT and encode as WebP.

    Tries WEBP_QUALITY first; steps quality down if the result is still over
    MAX_BYTES (a populated grid screenshot compresses far better than this in
    practice, but keep a margin rather than assume).
    """
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(raw_path) as im:
        im = im.convert("RGB")
        target = (CAPTURE_VIEWPORT["width"], CAPTURE_VIEWPORT["height"])
        if im.size != target:
            im = im.resize(target, Image.LANCZOS)
        quality = WEBP_QUALITY
        while True:
            im.save(OUT_PATH, "WEBP", quality=quality, method=6)
            size_bytes = OUT_PATH.stat().st_size
            if size_bytes <= MAX_BYTES or quality <= 40:
                break
            quality -= 10
        width, height = im.size
    raw_path.unlink(missing_ok=True)
    return OUT_PATH, width, height, size_bytes


def main():
    env = build_env()
    raw_path = capture_screenshot(env)
    out_path, width, height, size_bytes = run_step("OPTIMIZE", optimize, raw_path)
    log(
        f"\nwrote {out_path.relative_to(REPO_ROOT)} "
        f"({width}x{height}, {size_bytes / 1024:.1f} KB)"
    )


if __name__ == "__main__":
    main()
