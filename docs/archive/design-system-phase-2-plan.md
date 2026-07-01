# Design system — phase 2 plan (typography + remaining chrome)

Status: **COMPLETE — PRs A, B & C all shipped** · Drafted & finished 2026-06-30 ·
Follow-on to the **COMPLETE** `docs/archive/design-system-unification-plan.md` (phase 1:
PRs 1–3 + the nav-refresh #358). PR A = body typography (#363); PR B = footer
chrome (#366); PR C = phase-1 doc reconcile (this change). Decisions taken:
**Option A (system-ui)** for the body typeface, **Meso left on Hanken Grotesk**,
footer **token refresh + nav gutter alignment**.

## Why a phase 2

Phase 1 unified the site on one token-driven look and, in the #358 follow-up,
refactored the **nav** onto one shared `static/css/nav.css` rendered in a neutral
**`system-ui`** stack across both the main site and the Meso shell. That left two
visible loose ends and one housekeeping item:

1. The nav now reads as modern `system-ui`, but **every other element on the main
   site is still Verdana** (`base.css`'s universal `* { font-family: "Verdana" }`).
   A clean modern nav over Verdana body copy is the biggest remaining
   inconsistency.
2. The **footer** is still the old `.footer` chrome (Verdana, hard-coded
   `--main-color-*`), unlike the refreshed nav.
3. The phase-1 plan doc still describes PR 3 as the Meso-only
   `_meso_sitenav.html` partial that #358 **removed** — it's stale.

This plan scopes those so a fresh session can execute them cold.

## Goal

Finish the "reads as one modern site" job: one typeface system across the whole
site (main + Meso) and consistent top/bottom chrome, without adding build tooling
(stay on Path B — static CSS + WhiteNoise).

## PR A — Body typography unification (the big one) ✅ shipped (#363)

### Current state
- Typography is centralized in **one rule**: `base.css`
  `* { font-family: "Verdana", sans-serif; line-height: 1.5; font-size: 16px; … }`.
  Everything inherits Verdana from here.
- Only intentional exceptions: `monospace` on `.box.purchase` (the price button)
  and code/`pre` (~line 1611). **Preserve those.**
- Headings (`h1`–`h3`, `.cover > h1`, etc.) set only `font-size` via the modular
  scale (`--s2..--s4`); they inherit the family. No per-heading family to change.
- The main-site bases (`_base`, `_base_wide`, `_base_full`, `home`) load **no
  webfont** today. The Meso shell loads Hanken Grotesk + IBM Plex Mono; the nav
  (nav.css) already pins `system-ui`.

### The decision (confirm before building)
Which body typeface?
- **Option A — `system-ui` stack (recommended).** Reuse exactly what the nav now
  uses: `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`. Zero network
  cost, instantly consistent with the nav, native feel, lowest risk. Introduce a
  `--font` token in `:root` and point the `*` reset at it (`font-family: var(--font)`).
- **Option B — a loaded webfont (e.g. Inter, as in the spike).** More branded;
  matches `docs/spikes/basecoat/` exactly. Costs a font load on every page and
  introduces a *third* family alongside Meso's Hanken (unless Meso also moves).
- **Option C — adopt Meso's Hanken Grotesk site-wide.** True single font for main
  + Meso; biggest change (loads Hanken everywhere) and a larger visual shift.

Recommendation: **A**. It's the consistent, no-dependency continuation of #358.
Revisit B/C only if a branded display face is wanted later.

### Scope (Option A)
- `:root`: add `--font: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;`
  (and consider `--font-mono` for the existing monospace spots).
- `* { font-family: var(--font); }` (replace the bare Verdana). Keep the
  `monospace` exceptions (or point them at `--font-mono`).
- Sanity-check `line-height: 1.5` / `font-size: 16px` on `*` still read well in
  the new face; tune only if needed (don't scope-creep into a full type-scale
  rework).
- Meso is unaffected by the `base.css` change (its shell doesn't load base.css);
  decide separately whether to leave Meso on Hanken (fine — it's the app
  sub-brand) or also move it. Default: **leave Meso on Hanken.**

### Risks
- **Highest blast radius in the whole initiative** — touches every page's text.
  Verdana is wide; `system-ui` is narrower, so line wrapping, button widths, and
  vertical rhythm shift everywhere. Expect subtle reflow.
- Watch: the hero `.cover > h1`, `.box.purchase` (keep mono), tables, `.tag`/
  `.button` widths, forms, the testimonials block.

### Verification
- TDD guard (red→green), e.g. `pages/tests/test_design_system_phase2.py`:
  assert `base.css` defines `--font` and the `*` rule uses `var(--font)`, and that
  no bare `"Verdana"` remains except the intended exception(s).
- Browser before/after on home, store (seed with `seed_products`, dev port 8034),
  a product detail, challenges, an account/auth page, and a Meso page — desktop +
  mobile. Confirm no broken wrapping / overflow.
- Codex review loop; ship through the human merge/deploy gate (mastering.fitness).

## PR B — Footer chrome refresh (small) ✅ shipped (#366)

### Current state
`templates/_footer.html` = `.footer` (dark bar) with `.footer a` links;
`base.css` `.footer { color/background-color/padding }` uses `--main-color-*`
directly and inherits Verdana.

### Target
Bring the footer in line with the refreshed nav: pull colors from the shared
tokens, pick up the `--font` from PR A automatically, and (optional) match the
nav's horizontal padding/rhythm so top and bottom chrome feel like a pair.
Keep it a `<footer>`/`.footer` — no structural rewrite needed.

### Scope
- `.footer` / `.footer a`: token-driven colors (`--foreground`/`--nav-bg` family
  as appropriate), confirm it inherits `--font`. Optional: align padding with
  `.nav`'s `clamp(16px, 4vw, 40px)` inline padding.
- Could fold into PR A if small, but a separate tiny PR keeps diffs clean.

### Verification
Render `_footer.html` test + browser check on any page footer (incl. Meso, which
also shows the footer via the main bases? — confirm: Meso shell does **not**
include `_footer.html`; the main bases do). TDD + codex + gate as usual.

## PR C — Reconcile the phase-1 plan doc (housekeeping, docs-only) ✅ shipped

`docs/archive/design-system-unification-plan.md` still describes PR 3 as the Meso-only
`meso/_meso_sitenav.html` partial styled by `.meso-sitenav`. #358 **removed** that
in favor of the shared `nav.css` + `_nav.html` reused in `_meso_base` via
`{% block site_nav %}`. Update the PR 3 section (and the status header) to record
the #358 nav-refresh that superseded it. Docs-only → `paths-ignore` means no CI
deploy; trivial.

## Optional future track — Base Coat-proper

Phase 1 deliberately stayed on Path B (no Tailwind/Node). If a real component
library is later wanted (Meso would benefit most), adopting Base Coat-proper is a
separate, larger initiative: it adds tooling to the Django + WhiteNoise + CI
pipeline and warrants its own plan. Not part of phase 2.

## Suggested sequencing

1. **PR C** first (trivial, unblocks accurate docs).
2. **PR A** (typography) — the headline; do it deliberately with a full browser
   pass.
3. **PR B** (footer) — fast follow once `--font` exists.

## Conventions (carry over from phase 1)

- Red→green tests that read the real CSS / render real templates.
- `just test` + `just lint` + `pre-commit` (djhtml + biome) before pushing.
- Local static: WhiteNoise serves from `STATIC_ROOT`, so run
  `uv run python app/manage.py collectstatic --noinput` after CSS edits to see
  changes on the dev server (port 8034).
- Browser before/after on affected + inherited pages; codex review loop.
- Ship through the human merge/deploy gate; merge to `main` auto-deploys via
  GH Actions (Django CI → Deploy) to mastering.fitness.

## Decisions taken at kickoff (resolved)
- **Body typeface: A — `system-ui`** (the nav's stack), reused via a `--font`
  token. Shipped in PR A (#363).
- **Meso stays on Hanken Grotesk** (app sub-brand) — its shell doesn't load
  base.css, so PR A left it untouched.
- **Footer: token refresh *and* nav-gutter alignment** — moved onto
  `--nav-bg` / `--nav-fg` and picked up the nav's `clamp(16px, 4vw, 40px)`
  inline padding so top and bottom chrome pair. Shipped in PR B (#366).
