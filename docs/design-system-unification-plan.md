# Design-system unification — plan

Status: **PR 1 implemented** (Foundation + 3 pain points) · Started 2026-06-30 ·
Kicked off from issue #327. PRs 2–3 still to come.

## Goal

Make Mastering Fitness visually consistent by collapsing the competing styling
systems into **one token-driven system**, and reconnect the Meso app so it reads
as part of the same site.

## Root cause (why things look broken today)

The site runs ~3.5 competing styling systems:

| System | Reality |
| --- | --- |
| `static/css/base.css` (~2050 lines) | The actual live system — bespoke custom-property + Every-Layout design. Sets `--border-radius:12px`. |
| `static/css/bulma.min.css` (677 KB) | **Not linked by any template. Dead weight.** |
| `crispy_bulma` (`CRISPY_TEMPLATE_PACK="bulma"`) | Every `{{ form\|crispy }}` emits Bulma markup with **no Bulma CSS loaded** → skinny `<select>`, mismatched input/button heights, "broken" newsletter + challenges search/sort forms. |
| `static/css/meso.css` (`_meso_base.html`) | Separate shell, nav, fonts, oklch tokens → Meso looks like a different website. |

The navbar rounding (#327) was `.box` applying `border-radius:12px`; `_nav.html`
is `class="box invert navbar"`. Already fixed in **PR #342**.

## Decisions (locked)

- **Visual direction — "Faithful, made consistent":** B&W base, **black buttons
  kept**, the site's existing **steel-blue `#31759d`** accent (links, active-nav
  underline, focus rings, **soft tinted tags**), 8px radius, strong interaction
  affordances (hover lift + blue focus ring + pressed active; clickable challenge
  cards that lift and reveal a "View challenge →" hint).
- **Build path — Path B (token-refactor `base.css`, no new tooling).** No
  Tailwind/Node added to the Django + WhiteNoise + CI pipeline. Base Coat-proper
  stays an option later if its component library is wanted (Meso would benefit).
- **Sequencing:** PR 1 = Foundation + the 3 pain points; PR 2 = rest of main
  site; PR 3 = Meso reconnection (final phase).
- Spike that established the look: `docs/spikes/basecoat/` (`challenges*.html`,
  `meso.html`). Hand-built in the Base Coat / shadcn visual language.

## Token + component spec (target)

Introduce shadcn-style semantic tokens in `:root` (values already match the
locked look and the existing palette, so this is mostly aliasing):

```
--background:#fff;  --foreground:#0a0a0a;
--card:#fff;        --card-foreground:#0a0a0a;
--muted:#f4f4f5;    --muted-foreground:#6b7280;
--border:#e4e4e7;   --input:#d4d4d8;
--primary:#0a0a0a;  --primary-foreground:#fff;  --primary-hover:#262626;
--accent:#31759d;   --accent-deep:#1f516b;  --accent-soft:#eaf2f7;  --accent-line:#bcd6e4;
--ring:var(--accent);
--radius:8px;   /* replaces the 12px --border-radius */
```

Existing `--main-color-*` vars stay (re-pointed where helpful) so the ~2050 lines
that reference them keep working. Component rules updated to the locked visuals:

- `.box` → add `1px solid var(--border)` + `box-shadow` sm + `--radius`; keep all
  variants (`.invert`, `.transparent`, `.purchase`, `.owned`, `.login`).
- `.button` → keep black; add hover lift+lighten, `:focus-visible` accent ring,
  `:active` press, disabled state.
- `input, textarea, select` → unify height/padding, accent focus ring; ensure the
  native `<select>` matches input height (kills the "skinny dropdown").
- `.tag` → soft accent pill + hover/focus states.
- Clickable cards → linked challenge cards lift + title→accent + reveal hint.

## crispy decision

Drop `{{ form|crispy }}` for the 4 challenges templates and render fields with our
own component classes. Add the classes at the widget level (in `filters.py` /
the create/record forms) so templates stay clean, e.g. `class="input"` /
`class="select"`. Then remove `crispy_forms` + `crispy_bulma` from
`INSTALLED_APPS` and the `CRISPY_*` settings. (Footprint is tiny — 4 files.)

## PR-by-PR

### PR 1 — Foundation + the 3 pain points ✅ implemented
Done on branch `design-system-pr1-foundation`: semantic token layer added (radius
8px, accent/soft/line, primary-hover); `.box` / `.button` / inputs / `select` /
`.tag` / clickable `.card-box` re-skinned to the locked look with hover-lift,
accent focus rings, pressed active, disabled states; soft-pill category tags;
`bulma.min.css` (677 KB) deleted; crispy dropped from the 4 challenges templates
(rendered via `_form_fields.html` + widget classes from `form_styling.py`) and
removed from `INSTALLED_APPS` / settings / deps. Red→green tests in
`challenges/tests.py::DesignSystemPR1Tests`.

1. `base.css`: add token layer; re-skin `.box` / `.button` / inputs / `.tag` to
   the locked look + affordances. **This re-skins the whole site** (intended).
2. Delete dead `bulma.min.css`.
3. `/challenges`: drop crispy on the filter form (`challenge_filtered_list.html`
   + `sidebar.html`), render with component classes; make challenge cards
   clickable with hover affordance (`_single_challenge_card.html`,
   `_challenge_group.html`). Add widget classes in `filters.py`.
4. Also de-crispy `record_create.html` / `challenge_create.html` (same system),
   then remove crispy deps + settings.
5. **Regression pass** (browser): home, store, an account/auth page, a
   `.box.purchase`/`.box.login` page, product detail — they inherit the new
   `.box`/`.button`/`.tag` look automatically.

### PR 2 — Rest of main site
- Newsletter form (`_newsletter.html`) — fix the broken field/button layout.
- Store, home, account/auth, product/book pages — template-level cleanups beyond
  what the global re-skin already gave.

### PR 3 — Meso reconnection (final phase)
- Add the shared site nav to `_meso_base.html`.
- Point `meso.css` tokens at the shared core tokens (keep Meso's component
  density / workspace chrome). Validated by `docs/spikes/basecoat/meso.html`.

## Risks & mitigations

- **Global re-skin regresses un-edited pages** (store, account, `.box.purchase`,
  `.box.login`). → Browser regression pass in PR 1; preserve all `.box` variants.
- **crispy removal drops field/error rendering.** → Render labels + errors
  explicitly; manually test each form (submit, validation).
- **`.box` gaining a border/shadow** changes dense layouts. → Check `.box.invert`
  (nav/footer) and nested boxes specifically.
- Each PR is browser-verified and ships through the human merge/deploy gate.

## Verification

Per PR: `just test` + `just lint`, plus a Chrome pass on the affected + inherited
pages (before/after screenshots for the visual diff).
