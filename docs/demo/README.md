# Meso walkthrough demo video

**As of issue [#454](https://github.com/lancegoyke/fitness-store/issues/454),
the landing page does not show this video by default** — the recording's
quality wasn't good enough and it repeated visuals already on the page, and
the live, no-signup public sandbox at `/meso/demo/` (issue #389) is the
walkthrough now. The recorder below still works; publishing a new recording
and setting `MESO_DEMO_VIDEO_URL` (see `.env.example`) is what puts a video
back on the page.

`just record-demo` regenerates a short screen recording of the real Meso
product — no manual steps, no editing. It's the "level 1" demo for issue
[#388](https://github.com/lancegoyke/fitness-store/issues/388): plan at
[`docs/meso/demo-walkthrough-video-plan.md`](../meso/demo-walkthrough-video-plan.md).

## Prerequisites

```bash
just services                        # Postgres :5434 / Redis :6334
uv sync                              # installs Playwright as a dev dep
uv run playwright install chromium   # downloads a headless-capable Chromium
```

## Run it

```bash
just record-demo
```

That's the whole interface. Under the hood (`scripts/record_demo.py`) it:

1. Runs `migrate --no-input` then `seed_demo_recording --json` — a
   deterministic, known-password demo coach + Maya's built/delivered/logged
   plan (idempotent; safe to re-run).
2. Starts `runserver 8035 --noreload` in the background with
   `MESO_AGENT_FAKE=1` + `MESO_AGENT_RUN_SYNC=1` (and the Anthropic/VAPID env
   vars explicitly blanked) — the agent step is the curated, no-network
   `FakeDemoClient` proposal, never a real Anthropic/push call. Always
   terminated (own process group) when the script exits, success or failure.
3. Drives the storyboard below in headless Chromium via Playwright, recording
   video the whole time.
4. Writes `docs/demo/out/meso-walkthrough.mp4` (falls back to `.webm` if no
   ffmpeg is found — system `ffmpeg` is preferred; else Playwright's own
   bundled copy under `~/Library/Caches/ms-playwright/ffmpeg-*/` (macOS) /
   `~/.cache/ms-playwright/ffmpeg-*/` (Linux) is used automatically).

`docs/demo/out/` is git-ignored — **commit the tool, never the binary**; host
the produced video wherever it's shown, and regenerate it whenever the UI
changes enough to make the old one stale.

## The storyboard-step editing model

Each step is a small, labelled function in `scripts/record_demo.py`
(`step_roster`, `step_athlete_profile`, `step_designer`,
`step_agent_proposal`, `step_review`, `step_deliver`,
`step_athlete_logs_a_set`) — one step, one function, so a UI change is a
one-line/one-function edit, not a re-shoot:

1. **Roster** — land on `/meso/`; Maya Okonkwo's row is visible.
2. **Athlete profile** — open Maya; her delivered program + logged session.
3. **Designer** — open her plan; the current week's grid + agent composer.
4. **Agent proposal** — type a coach instruction, send it, follow the
   resolved batch's review link (the pre-baked `MESO_AGENT_FAKE` proposal —
   see below).
5. **Review** — the proposed changes are visible; click "Apply … & deliver".
6. **Deliver** — click "Deliver to Maya …"; wait for the confirmation card.
7. **Athlete view** — log out, log back in as Maya (same recorded browser
   context/viewport — a second `record_video_dir` context would produce a
   *second* video file), open a not-yet-logged session, toggle a set, "Save
   progress".

Synchronization is always a Playwright auto-wait on a selector
(`expect(...)`/`wait_for_*`) — never a sleep. `pause(page, seconds)` exists
only to hold a screen long enough to read on camera; it's never load-bearing.

## The pre-baked agent step

The agent turn (`MESO_AGENT_FAKE=1`) never calls Anthropic — it grounds a
small, curated `FakeDemoClient` proposal on the plan's own real rows (a
knee-safe swap honoring Maya's contraindication, a load progression, a volume
trim), so it's deterministic, free, and still passes the real review-gate
validation. See `app/store_project/meso/agent/fake.py`.

## Enumerated `data-testid`s

Added only where role/text selectors were brittle or ambiguous (role/text
selectors were kept everywhere else — e.g. the "Sign In" button, "Open in
designer" link, and Maya's roster row, scoped to `a.meso-row` since her name
also appears as a plain link in the roster's "Recent activity" rail):

| `data-testid` | Element | Template |
|---|---|---|
| `agent-composer-input` | Agent chat message input | `meso/designer.html` |
| `agent-composer-send` | Agent chat send button | `meso/designer.html` |
| `agent-review-link` | "Review N changes →" link on a resolved agent turn | `meso/designer.html` |
| `review-apply` | "Apply … & deliver →" button | `meso/review.html` |
| `deliver-send` | "Deliver to {athlete} …" button | `meso/deliver.html` |
| `set-toggle` | Per-set "mark done" circle (one per set row) | `meso/athlete_session.html` |
| `session-save` | "Save progress" button | `meso/athlete_session.html` |

Two selector gotchas worth knowing if a future edit touches these flows:

- **`agent-review-link` is not unique.** The designer restores a plan's
  *persisted* chat history on load (`hydrateThread` in `static/js/meso.js`),
  and `seed_demo_recording` reseeds the demo workspace without clearing old
  `AgentProposalBatch` rows — so every past recording run leaves another
  review link in that history. The script always takes `.last` (the newest
  turn, appended at the end of the thread).
- **The Django Debug Toolbar silently eats clicks.** `config/settings/local.py`
  turns it on whenever `DEBUG` is on (required for `seed_demo_recording`),
  and its floating handle sits on top of real controls (e.g. "Open in
  designer"), intercepting the click rather than just looking ugly. The
  script hides `#djDebug` via a `context.add_init_script` scoped to the
  *recorded* browser context only (not a settings change, so `just dev` is
  unaffected) — deferred to `DOMContentLoaded`, since an init script runs
  before the HTML parser creates `document.documentElement`.
