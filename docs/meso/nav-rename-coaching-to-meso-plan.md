# Meso — rename top-nav "Coaching" → "Meso"

Status: **PLANNED** — not started. Tracking issue: _(add issue link when filed)_.

## Why

The only path from the site to the Meso app is a top-nav link labelled
**"Coaching"**. On a personal trainer's site that reads as "hire a coach," not
"open our app," so first-time visitors don't discover Meso. Relabel the visible
text to **"Meso"**. The URL/route is unchanged.

## Scope (small, surgical)

- **Change the anchor text** in `app/store_project/templates/_nav.html:15` from
  `Coaching` to `Meso`. The link stays `{% url 'meso:roster' %}` (`/meso/`) and
  keeps its `active` highlight on the `meso` namespace.
- **Update the two tests** that assert the literal nav string:
  - `app/store_project/pages/tests/test_design_system_pr3.py:137–138` and `:160`
  - `app/store_project/meso/tests/test_design_reconnect.py:96–99`
  (Docstring/comment mentions of "Coaching" in those test files can be tidied too.)

## Explicitly out of scope / leave alone

- **No mobile-nav duplicate.** `_nav.html` uses one `.nav-links` container
  toggled via a CSS checkbox — there is a single link to change.
- **`templates/meso/landing.html:23`** — "Coaching, periodized" is marketing
  copy (eyebrow), **not** a nav label. Leave it.
- The footer has no "Coaching" reference.
- The URL/route name (`meso:roster`) does **not** change.

## Note for reviewer — duplicate "Meso" wordmark?

The Meso workspace sub-header already renders a **"Meso"** wordmark
(`templates/meso/_meso_base.html:35–38`), but that sub-header appears **only on
Meso pages**. Renaming the top-nav link to "Meso" therefore creates no
duplication off-Meso; on Meso pages the active top-nav "Meso" alongside the
workspace wordmark is consistent, not confusing.

## Acceptance criteria

- The top nav shows **"Meso"** linking to `/meso/`, active-highlighted on the
  `meso` namespace.
- Both updated tests pass; `just test` green.
- No other visible "Coaching" **nav** labels remain (marketing copy untouched).

## Risk

Trivial. The only trap is the two **exact-HTML** test assertions — update them in
the same change or the suite breaks.
