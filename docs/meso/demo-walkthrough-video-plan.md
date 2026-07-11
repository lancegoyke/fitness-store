# Meso — Level-1 walkthrough demo video (automated, re-recordable)

Status: **SHIPPED** — issue [#388](https://github.com/lancegoyke/fitness-store/issues/388) is done. `just record-demo` (`scripts/record_demo.py`) seeds deterministic demo data, drives the full storyboard below in headless Chromium via Playwright, and writes `docs/demo/out/meso-walkthrough.mp4` — zero manual steps, re-recordable after any UI change. See `docs/demo/README.md` for prerequisites, the enumerated `data-testid`s, and the storyboard-step editing model.

**Not currently on the landing page**, though: issue [#454](https://github.com/lancegoyke/fitness-store/issues/454) turned the embed off (the recording was confusing and repeated the page's other visuals) — the live, no-signup sandbox at `/meso/demo/` (issue #389) is the walkthrough cold visitors get now. The recorder and publisher still work; re-enabling is `just record-demo && just publish-demo-video`, then setting `MESO_DEMO_VIDEO_URL`.

## Why

A new, non-subscriber visitor currently cannot see Meso in action without
creating an account: the public pages (`/meso/`, `/meso/coach/`) are text-only —
0 screenshots, 0 video (verified live). The cheapest "level 1" demo is a short
screen recording of the real product being used, which is far easier to consume
than signing up.

The catch: **the UI is still changing.** A hand-recorded video goes stale the
moment a button moves, and nobody re-records by hand. So the deliverable is not
a video — it's a **tool that regenerates the video on demand** from a scripted
browser session, runnable with one command.

This is the sibling of the [public sandbox demo](./public-sandbox-demo-plan.md)
("level 2"). Both share the **pre-baked agent** mechanism (V4 below).

## Definition of done

- `just record-demo` (new recipe) seeds deterministic demo data, drives a
  scripted flow through the Meso coach workspace in a real browser, and writes a
  video file — with **zero manual steps**.
- Re-running after a UI change produces an equivalent video by editing a
  readable, labelled storyboard — not by re-shooting.
- **No** real Anthropic, Stripe, email, or push calls happen during recording.
  The agent step shows a fixed, curated proposal.

## Decisions (recommended; proceed unless overridden)

| # | Decision | Choice |
|---|----------|--------|
| V1 | **Tooling** | **Playwright (Python).** Native video recording (`record_video_dir`), headless-capable, auto-waiting selectors, and it matches the uv/pytest stack. Nothing browser-automation-related exists in the repo today (checked `pyproject.toml`), so add Playwright as a **dev** dependency. |
| V2 | **Demo data** | Reuse `store_project.meso.demo.load_demo()` — it's idempotent (`@transaction.atomic`, upsert-by-natural-key) and built from fixed constants (`ATHLETES`, `SAMPLE_PLAN`, `GROUP` in `management/commands/seed_meso_demo.py`), so records are reproducible. |
| V3 | **Auth for the browser** | Add a management command (e.g. `seed_demo_recording`) that creates a known coach `User` (email/password from env), `CoachProfile`, and calls `load_demo()`. The Playwright script logs in through the real allauth form **before** recording starts (login happens off-camera). Alternative: mint a session cookie server-side and inject it. |
| V4 | **Agent step** | **Pre-baked proposal.** A demo/recording mode (`MESO_AGENT_FAKE=1` setting, or a "demo mode" flag) short-circuits `agent_propose()` / `_reserve_plan_draft()` to return a fixed curated `ProposalBatch`/`ChangeSet` with **no** Anthropic call. Deterministic, free, and reused by the sandbox plan. |
| V5 | **Output** | MP4 (Playwright native), written to a **git-ignored** artifact dir (`docs/demo/out/` or `build/`), optional GIF via `ffmpeg`. **Do not commit the binary** — commit the tool and regenerate. Host the produced video wherever it's shown. |

## Storyboard (the scripted flow)

Each step: navigate → wait on a stable selector → brief legibility pause →
(optional) caption overlay.

1. Land on `/meso/` roster — 5 seeded athletes + a group already visible.
2. Open an athlete (Maya Okonkwo) → her delivered program + logged session.
3. Open the **designer** → a periodized week (loads / %1RM).
4. Trigger the **AI agent** → the pre-baked proposal appears ("the agent proposes").
5. **Approve** the proposal → it applies ("the coach approves").
6. **Deliver** a week to the athlete/group → confirmation (send is stubbed).
7. _(Optional)_ Switch to the athlete phone view `/meso/me/` and log a set.

## Making it re-recordable (the core requirement)

- **Stable selectors.** Drive the script off `data-testid` / ARIA-role
  selectors, never pixel coordinates or brittle CSS. This means adding a small,
  enumerated set of `data-testid` attributes to the elements the script touches
  (minor template edits) so restyles don't break the run.
- **Auto-waiting, not sleeps.** Wait on selectors/network-idle so perf changes
  don't desync the recording.
- **Deterministic data + agent** (V2, V4).
- **One command.** `just record-demo` = seed → run → output.
- **Readable storyboard.** The script encodes the 7 steps as labelled blocks so
  updating a step after a UI change is a one-line edit.

## Phases

1. **Plumbing** — add Playwright dev dep + `playwright install chromium` note;
   `just record-demo`; `seed_demo_recording` command; the `MESO_AGENT_FAKE`
   short-circuit in the agent path (V4).
2. **The script** — `scripts/record_demo.py` implementing the storyboard,
   writing MP4 to the artifact dir.
3. **Polish** — captions/cursor/zoom, GIF export, a short `docs/demo/README.md`.

## Key files & pointers

- `justfile` — recipe pattern is `uv run python app/manage.py <cmd>`; dev server
  on `:8034`; `just services` brings up DB `:5434` / Redis `:6334`.
- `app/store_project/meso/demo.py` — `load_demo()` / `clear_demo()` (reuse for seed).
- `app/store_project/meso/management/commands/seed_meso_demo.py` — fixed
  `ATHLETES` / `SAMPLE_PLAN` / `GROUP` constants.
- `app/store_project/meso/views.py:2332` `agent_propose()` and `:622`
  `_reserve_plan_draft()` — the pre-baked short-circuit point.
- New: `scripts/record_demo.py` (scripts dir currently holds only `backup.sh`),
  a `record-demo` just recipe, `seed_demo_recording` mgmt command.

## Risks / open questions

- **Binary storage.** Don't commit the MP4 (repo bloat) — commit the tool,
  gitignore the output, host the video where it's used. Confirm the host.
- **Playwright browser install** in CI/dev — document `playwright install chromium`.
- **`data-testid` churn** — keep the added attributes minimal and named for the
  storyboard steps.
