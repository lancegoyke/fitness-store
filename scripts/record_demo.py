#!/usr/bin/env python3
"""Regenerate the Meso walkthrough demo video (issue #388).

Standalone driver script — not a Django management command, not part of the
pytest suite. Zero manual steps: seed deterministic demo data, drive the real
coach + athlete flow in a headless, screen-recorded Chromium via Playwright,
and write ``docs/demo/out/meso-walkthrough.mp4``.

Run via ``just record-demo`` (``uv run python scripts/record_demo.py``), with
the dev Postgres/Redis containers already up (``just services``).

Storyboard (docs/meso/demo-walkthrough-video-plan.md), one step = one small,
labelled function below — the intended unit of edit when a UI change breaks a
selector:

    1. Roster           — the coach's athlete list.
    2. Athlete profile  — Maya's delivered program + logged session.
    3. Designer         — the current week's grid + agent chat.
    4. Agent proposal   — a coach instruction resolves to the pre-baked
                           ``MESO_AGENT_FAKE`` batch; follow the review link.
    5. Review           — apply the batch (redirects to deliver).
    6. Deliver          — send the week to Maya.
    7. Athlete view     — log in as Maya, toggle a set, save progress.

Synchronization is always a Playwright auto-wait on a selector
(``expect``/``wait_for``); ``pause()`` only slows the recording for
legibility and is never load-bearing for correctness.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGE_PY = REPO_ROOT / "app" / "manage.py"
OUT_DIR = REPO_ROOT / "docs" / "demo" / "out"
VIDEO_BASENAME = "meso-walkthrough"
SERVER_LOG_PATH = OUT_DIR / "record_demo.server.log"

# Kept a constant, dedicated port (not the dev-server's :8034) so a stray
# `just dev` running alongside this script never collides with it.
PORT = 8035
BASE_URL = f"http://127.0.0.1:{PORT}"
SERVER_READY_TIMEOUT = 40  # seconds

VIEWPORT = {"width": 1280, "height": 720}

# Legibility pauses — cosmetic pacing only, see the module docstring.
BEAT = 1.0  # seconds
LONG_BEAT = 2.0  # seconds; used where a screen most needs to "read" on camera

# The coach instruction typed into the agent composer (STEP 4). Chosen to echo
# Maya's seeded contraindication so the pre-baked FakeDemoClient's swap +
# honors line read as a direct response to it on camera.
AGENT_INSTRUCTION = (
    "Her left knee has been cranky — keep this week knee-friendly but keep "
    "her progressing."
)

# Server env for the recording: MESO_AGENT_FAKE + MESO_AGENT_RUN_SYNC make the
# agent step a fast, deterministic, no-network call; the Anthropic/VAPID vars
# are explicitly blanked (even though a real .env may set them) so a bug in
# the fake-client gate can never fire a real network call during an
# unattended recording. `load_dotenv()` (config/settings/base.py) never
# overrides already-exported vars, so these win over `.env`.
SERVER_ENV_OVERRIDES = {
    "MESO_AGENT_FAKE": "1",
    "MESO_AGENT_RUN_SYNC": "1",
    "ANTHROPIC_API_KEY": "",
    "MESO_VAPID_PUBLIC_KEY": "",
    "MESO_VAPID_PRIVATE_KEY": "",
}

# Hides the Django Debug Toolbar (config/settings/local.py, on whenever DEBUG
# is on) inside the *recorded* browser context only — a purely cosmetic fix
# scoped to this script, not a settings change that would affect `just dev`.
# Its floating handle also sits on top of real controls (e.g. "Open in
# designer"), silently eating clicks — so this is load-bearing, not just
# cosmetic. Deferred to DOMContentLoaded: an init script runs *before* the
# HTML parser creates `document.documentElement`, so appending to it (or
# `document.head`) any earlier throws (caught, but the style never lands).
HIDE_DEBUG_TOOLBAR_CSS = """
document.addEventListener("DOMContentLoaded", () => {
  const style = document.createElement("style");
  style.textContent = "#djDebug{display:none !important;}";
  document.head.appendChild(style);
});
"""


class StepError(RuntimeError):
    """A storyboard step failed; carries the step name for a clear exit message."""

    def __init__(self, step: str, original: BaseException) -> None:
        super().__init__(f"{step}: {original}")
        self.step = step
        self.original = original


def log(message: str) -> None:
    print(message, flush=True)


def run_step(name, fn, *args, **kwargs):
    """Run one storyboard step, tagging any failure with its label."""
    log(f"\n=== {name} ===")
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        raise StepError(name, exc) from exc
    log("    ok")
    return result


def pause(page: Page, seconds: float = BEAT) -> None:
    """Hold the current screen for ``seconds`` — legibility only, never sync."""
    page.wait_for_timeout(seconds * 1000)


# ---------------------------------------------------------------------------
# Setup: migrate + seed (plain subprocess calls, not the long-running server)
# ---------------------------------------------------------------------------


def run_management_command(args, env):
    cmd = ["uv", "run", "python", str(MANAGE_PY), *args]
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"`{' '.join(args)}` exited {result.returncode}")
    return result.stdout


def seed_demo_data(env):
    run_management_command(["migrate", "--no-input"], env)
    stdout = run_management_command(["seed_demo_recording", "--json"], env)
    data = None
    for line in stdout.splitlines():
        candidate = line.strip()
        if candidate.startswith("{"):
            data = json.loads(candidate)
            break
    if not data:
        raise RuntimeError(
            f"seed_demo_recording --json printed no JSON object:\n{stdout}"
        )
    coach_email = data["coach_email"]
    athlete_email = data.get("athlete_email")
    if not athlete_email:
        raise RuntimeError(
            "seed_demo_recording didn't return an athlete_email — Maya's demo "
            "row wasn't found (see its stderr warning above)."
        )
    log(f"    coach email:   {coach_email}")
    log(f"    athlete email: {athlete_email}")
    return coach_email, athlete_email


# ---------------------------------------------------------------------------
# Dev server subprocess (own process group so it can never outlive this script)
# ---------------------------------------------------------------------------


def start_server(env):
    cmd = ["uv", "run", "python", str(MANAGE_PY), "runserver", str(PORT), "--noreload"]
    log(f"$ {' '.join(cmd)}")
    log_file = open(SERVER_LOG_PATH, "w")
    proc = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._log_file = log_file  # keep the handle open; closed in stop_server()
    return proc


def wait_for_server(proc, url, timeout=SERVER_READY_TIMEOUT):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"dev server exited early (code {proc.returncode}) — see {SERVER_LOG_PATH}"
            )
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.3)
    raise TimeoutError(f"dev server never answered {url} ({last_error})")


def stop_server(proc):
    if proc is None:
        return
    log_file = getattr(proc, "_log_file", None)
    if proc.poll() is None:
        log("\n=== stopping dev server ===")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    if log_file:
        log_file.close()


# ---------------------------------------------------------------------------
# Off-camera login (STEP 0 — not part of the recorded video)
# ---------------------------------------------------------------------------


def offcamera_login(browser, email, password):
    """Log in through the real allauth form in an unrecorded context.

    Returns the resulting ``storage_state`` so the recorded context can start
    already authenticated — the login form itself never appears on camera.
    """
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    page.goto(f"{BASE_URL}/accounts/login/")
    page.locator('input[name="login"]').fill(email)
    page.locator('input[name="password"]').fill(password)
    with page.expect_navigation(
        url=lambda u: "/accounts/login/" not in u, timeout=15000
    ):
        page.get_by_role("button", name="Sign In").click()
    state = context.storage_state()
    context.close()
    return state


# ---------------------------------------------------------------------------
# Storyboard steps (recorded)
# ---------------------------------------------------------------------------


def step_roster(page: Page):
    """STEP 1 ROSTER — land on the coach's athlete list.

    Scoped to ``a.meso-row`` (not just role+name) because "Maya Okonkwo" also
    appears as a plain link in the roster's "Recent activity" rail — role/text
    alone resolves to both and Playwright's strict mode rejects the ambiguity.
    """
    page.goto(f"{BASE_URL}/meso/")
    maya_row = page.locator("a.meso-row", has_text="Maya Okonkwo")
    expect(maya_row).to_be_visible()
    pause(page, LONG_BEAT)
    with page.expect_navigation(url=re.compile(r"/meso/athlete/")):
        maya_row.click()


def step_athlete_profile(page: Page):
    """STEP 2 ATHLETE PROFILE — Maya's delivered program + logged session."""
    expect(page.get_by_role("heading", name="Maya Okonkwo")).to_be_visible()
    open_designer = page.get_by_role("link", name="Open in designer")
    expect(open_designer).to_be_visible()
    pause(page, LONG_BEAT)
    with page.expect_navigation(url=re.compile(r"/meso/designer/")):
        open_designer.click()


def step_designer(page: Page):
    """STEP 3 DESIGNER — the periodized current week + agent composer."""
    expect(page.get_by_test_id("agent-composer-input")).to_be_visible()
    pause(page, LONG_BEAT)


def step_agent_proposal(page: Page, instruction: str):
    """STEP 4 AGENT PROPOSAL — a coach instruction resolves to a review link.

    ``seed_demo_recording`` resets the workspace (``clear_demo`` →
    ``load_demo``), so the designer's persisted chat thread starts empty and
    this run's proposal is the only ``agent-review-link`` on the page.
    ``.last`` is kept as belt-and-braces: the designer restores persisted
    history on load (``hydrateThread``/``serialize_chat_thread``), and the
    freshest turn is always appended at the end of the thread.
    """
    composer = page.get_by_test_id("agent-composer-input")
    composer.fill(instruction)
    pause(page, BEAT)
    page.get_by_test_id("agent-composer-send").click()
    # MESO_AGENT_RUN_SYNC resolves the batch inline, but this still crosses two
    # HTTP round-trips (POST + one status poll) plus Alpine's render — a
    # generous timeout costs nothing on a fast, deterministic fake client.
    review_link = page.get_by_test_id("agent-review-link").last
    expect(review_link).to_be_visible(timeout=30000)
    pause(page, LONG_BEAT)
    with page.expect_navigation(url=re.compile(r"/meso/review/")):
        review_link.click()


def step_review(page: Page):
    """STEP 5 REVIEW — let the proposal read, then apply & deliver."""
    apply_button = page.get_by_test_id("review-apply")
    expect(apply_button).to_be_visible()
    expect(apply_button).to_be_enabled()
    pause(page, LONG_BEAT)
    with page.expect_navigation(url=re.compile(r"/meso/deliver/")):
        apply_button.click()


def step_deliver(page: Page):
    """STEP 6 DELIVER — send the week to Maya; wait for the confirmation card."""
    deliver_button = page.get_by_test_id("deliver-send")
    expect(deliver_button).to_be_visible()
    pause(page, LONG_BEAT)
    deliver_button.click()
    expect(
        page.get_by_role("heading", name=re.compile(r"Delivered to Maya"))
    ).to_be_visible()
    pause(page, LONG_BEAT)


def step_athlete_logs_a_set(page: Page, athlete_email: str, password: str):
    """STEP 7 ATHLETE LOGS A SET — same recorded context/viewport, new identity.

    ``ACCOUNT_LOGOUT_ON_GET`` is on, so a bare GET drops the coach's session;
    logging back in as Maya in the *same* page/context keeps this in one
    recorded video (a second ``browser.new_context(record_video_dir=...)``
    would produce a second file).
    """
    page.goto(f"{BASE_URL}/accounts/logout/")
    page.goto(f"{BASE_URL}/accounts/login/")
    page.locator('input[name="login"]').fill(athlete_email)
    page.locator('input[name="password"]').fill(password)
    with page.expect_navigation(
        url=lambda u: "/accounts/login/" not in u, timeout=15000
    ):
        page.get_by_role("button", name="Sign In").click()

    page.goto(f"{BASE_URL}/meso/me/")
    # The already-logged "Lower" day shows "Logged"; pick a not-yet-logged day
    # so the toggle below is a real unchecked → checked flip on camera.
    todo_session = page.locator("a.meso-row", has_text="To do").first
    expect(todo_session).to_be_visible()
    pause(page, BEAT)
    with page.expect_navigation(url=re.compile(r"/meso/me/session/")):
        todo_session.click()

    toggle = page.get_by_test_id("set-toggle").first
    expect(toggle).to_be_visible()
    pause(page, BEAT)
    toggle.click()
    pause(page, BEAT)

    page.get_by_test_id("session-save").click()
    # Exact text: "Saved offline — will sync…" (the queued/offline state) also
    # starts with "Saved", which a substring match would ambiguously resolve.
    expect(page.get_by_text("Saved ✓", exact=True)).to_be_visible()
    pause(page, LONG_BEAT)


# ---------------------------------------------------------------------------
# Video post-processing
# ---------------------------------------------------------------------------


def find_ffmpeg():
    """System ``ffmpeg`` if on PATH, else Playwright's own bundled binary.

    Playwright's ffmpeg is *not* under the pip package's ``driver/`` dir (that
    only holds the Node driver) — it's under the same per-OS cache directory
    ``playwright install`` downloads browsers into (``ms-playwright``), as
    ``ffmpeg-<rev>/ffmpeg-<platform>``.
    """
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    candidates = []
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_path and browsers_path != "0":
        candidates.append(Path(browsers_path))
    candidates += [
        Path.home() / "Library" / "Caches" / "ms-playwright",  # macOS
        Path.home() / ".cache" / "ms-playwright",  # Linux
    ]
    for base in candidates:
        if not base.is_dir():
            continue
        for path in sorted(base.glob("ffmpeg-*/ffmpeg*")):
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return None


def probe_duration_seconds(ffmpeg_path, media_path):
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path and ffmpeg_path:
        candidate = Path(ffmpeg_path).with_name(
            Path(ffmpeg_path).name.replace("ffmpeg", "ffprobe")
        )
        if candidate.is_file():
            ffprobe_path = str(candidate)
    if not ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return round(float(result.stdout.strip()), 1)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def finalize_video(raw_webm_path: Path):
    """Move the recorded webm into place, then convert to mp4 if possible."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    final_webm = OUT_DIR / f"{VIDEO_BASENAME}.webm"
    shutil.move(str(raw_webm_path), final_webm)

    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        log(
            "\nNOTE: no ffmpeg found (system PATH or Playwright's bundled copy) — "
            f"kept the raw {final_webm.name}. Install ffmpeg and re-run to get the "
            "target .mp4 artifact."
        )
        return final_webm, probe_duration_seconds(None, final_webm)

    final_mp4 = OUT_DIR / f"{VIDEO_BASENAME}.mp4"
    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(final_webm),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(final_mp4),
    ]
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        log(f"\nNOTE: ffmpeg conversion failed — kept the raw {final_webm.name}.")
        return final_webm, probe_duration_seconds(ffmpeg_path, final_webm)

    final_webm.unlink(missing_ok=True)
    return final_mp4, probe_duration_seconds(ffmpeg_path, final_mp4)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_env():
    env = os.environ.copy()
    env.update(SERVER_ENV_OVERRIDES)
    return env


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for stray in OUT_DIR.glob("*.webm"):
        if stray.name != f"{VIDEO_BASENAME}.webm":
            stray.unlink()

    env = build_env()
    server_proc = None
    exit_code = 0
    video_result = None

    try:
        coach_email, athlete_email = run_step("SEED DEMO DATA", seed_demo_data, env)
        server_proc = start_server(env)
        run_step(
            "WAIT FOR DEV SERVER",
            wait_for_server,
            server_proc,
            f"{BASE_URL}/accounts/login/",
        )

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                password = env.get("MESO_DEMO_COACH_PASSWORD", "meso-demo-recording")
                storage_state = run_step(
                    "OFF-CAMERA LOGIN", offcamera_login, browser, coach_email, password
                )

                context = browser.new_context(
                    storage_state=storage_state,
                    viewport=VIEWPORT,
                    record_video_dir=str(OUT_DIR),
                    record_video_size=VIEWPORT,
                )
                context.add_init_script(HIDE_DEBUG_TOOLBAR_CSS)
                page = context.new_page()

                run_step("STEP 1 ROSTER", step_roster, page)
                run_step("STEP 2 ATHLETE PROFILE", step_athlete_profile, page)
                run_step("STEP 3 DESIGNER", step_designer, page)
                run_step(
                    "STEP 4 AGENT PROPOSAL",
                    step_agent_proposal,
                    page,
                    AGENT_INSTRUCTION,
                )
                run_step("STEP 5 REVIEW", step_review, page)
                run_step("STEP 6 DELIVER", step_deliver, page)
                run_step(
                    "STEP 7 ATHLETE LOGS A SET",
                    step_athlete_logs_a_set,
                    page,
                    athlete_email,
                    password,
                )

                video = page.video
                context.close()
                raw_webm_path = Path(video.path())
            finally:
                browser.close()

        video_result = run_step("FINALIZE VIDEO", finalize_video, raw_webm_path)

    except StepError as exc:
        log(f"\nFAILED at step: {exc.step}\n{exc.original}")
        exit_code = 1
    except Exception as exc:  # setup/server-lifecycle failures, no step label
        log(f"\nFAILED (setup): {exc}")
        exit_code = 1
    finally:
        stop_server(server_proc)

    if exit_code:
        sys.exit(exit_code)

    final_path, duration = video_result
    size_kb = final_path.stat().st_size / 1024
    duration_note = (
        f"{duration} seconds" if duration is not None else "duration unknown"
    )
    log(
        f"\nwrote {final_path.relative_to(REPO_ROOT)} ({duration_note}, {size_kb:.0f} KB)"
    )


if __name__ == "__main__":
    main()
