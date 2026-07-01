# Design system — phase 3 plan (auth pages → basecoat login card)

Status: **✅ COMPLETE — all three PRs shipped & deployed.** Drafted 2026-06-30 ·
Implements issue **#352 "[UI] Redesign auth pages"** · Follow-on to the
**COMPLETE** `docs/design-system-unification-plan.md` (phase 1: PRs 1–3 +
nav-refresh #358) and **COMPLETE** `docs/design-system-phase-2-plan.md` (PR A
body typography #363, PR B footer #366, PR C docs #367).

**Shipped:**
- **PR A** — auth card shell (`account/base_auth_card.html`) + login page + the
  gap CSS (`.button.block`, `.auth-card*`, tokenized `.or-separator`). #372.
- **PR B** — signup + password-reset request + reset-from-key onto the card
  (#374); signup `?next` passthrough follow-up (#375).
- **PR C** — the second-tier auth pages (password change/set, email manager, the
  confirmation + notice pages, the `socialaccount/*` confirm/connections pages)
  onto the card. #376. Template migration only, no new CSS, no migration; Codex
  review CLEAN; prod-verified (`/accounts/confirm-email/…/`,
  `/accounts/3rdparty/login/cancelled/` render the card live).

With PR C the **entire django-allauth auth surface** wears the one basecoat
login-card look. (Adopting Base Coat-proper — Tailwind + a Node build — remains
the separate, larger initiative the phase-2 plan deferred; not this phase.)

**Approach: Path B (static CSS only — no Tailwind/Node), consistent with phases
1–2.** We reproduce the *look* of basecoat's login-card component by composing
the design-system components that already shipped (`.box`, `.button`, element
`input`, `.tag`) against the existing `:root` tokens. No new dependencies, no
build tooling, no allauth form rewrites. (Adopting Base Coat-proper — Tailwind +
a Node build — remains the separate, larger initiative the phase-2 plan deferred;
explicitly **not** this phase.)

## Why a phase 3

Phases 1–2 unified the site chrome and body copy: one token-driven look, one
shared nav (`nav.css`, #358), a `system-ui` body face sitewide (#363), and a
token-driven footer paired with the nav (#366). The **auth pages are the last
major surface still wearing the pre-unification look** — ad-hoc `.box.login`
boxes, an `.or-separator` built from hardcoded greys, and `.stack-auth-form`
layout that predates the card system. Issue #352 points at basecoat's login-card
component as the target. Everything needed to build it already exists in
`base.css`; this phase mostly *composes* it.

## Goal

Bring the django-allauth auth pages onto the locked design language as a
basecoat-style **login card**, reusing the already-shipped `.box` / `.button` /
`input` / `.tag` components and `:root` tokens. Start with the two highest-traffic
pages (login, signup), then bring the password-reset flow and the second-tier
account pages into the same visual family. Add only the handful of tiny static-CSS
rules the card genuinely lacks.

## The reference — basecoat login card

Issue #352 links `https://basecoatui.com/components/card/`. The login-card example
(fetched verbatim) is a card with three regions:

```html
<div class="card">
  <header>
    <h2>Login to your account</h2>
    <p>Enter your email below to login to your account</p>
    <div class="card-action"><button class="btn" data-variant="link">Sign Up</button></div>
  </header>
  <section>
    <form class="grid gap-6">
      <div class="grid gap-2">
        <label class="label" for="email">Email</label>
        <input class="input" type="email" placeholder="m@example.com" required />
      </div>
      <div class="grid gap-2">
        <div class="flex items-center">
          <label class="label" for="password">Password</label>
          <a href="#" class="ms-auto text-sm ...hover:underline">Forgot your password?</a>
        </div>
        <input class="input" type="password" required />
      </div>
    </form>
  </section>
  <footer class="flex-col gap-2">
    <button class="btn w-full">Login</button>
    <button class="btn w-full" data-variant="outline">Login with Google</button>
  </footer>
</div>
```

Structure to mimic: **card header** (title + one-line muted description + a
subordinate Sign-Up link), **card body** (labelled email + password fields, with
"Forgot your password?" sitting inline to the right of the Password label),
**card footer** (a full-width primary submit, then a full-width outline social
button). We reproduce this with our own class vocabulary — see the mapping below.

### basecoat → this repo's shipped idiom

| basecoat class | our shipped equivalent |
| --- | --- |
| `.card` | `.box` (1px `--border`, 8px `--radius`, soft `0 1px 2px` shadow, `--s1` padding) |
| `.card` `<header><h2>` | `<h1>`/`<h2>` heading (already `.text-center` for `h1`) |
| `.card` `<p>` description | muted subtitle — reuse `.help-text` / `--muted-foreground`, or one new rule |
| `.btn w-full` (primary) | `.button` (black `--primary`) **+ a new full-width variant** |
| `.btn w-full data-variant="outline"` | `.button.outline` **+ the same full-width variant** |
| `.input` | bare `input` element selector (already 38px + steel-blue focus ring) |
| `.label` | `<label class="stack"><span>…</span>` (renders bold via `form label`) |
| "Forgot your password?" | `a.secondaryAction` (already present) |
| Sign-Up link | plain accent `<a>` (already present) |

**No new utility classes or tokens are needed.** The card composes already-shipped
semantic components; the only genuinely new CSS is a full-width button variant and
a couple of small polish rules (below).

## Current state (from a full discovery pass)

### Auth template wiring
- **allauth 65.13.1**, `socialaccount` extra. Email-only login
  (`ACCOUNT_LOGIN_METHODS = {"email"}`); signup collects `email* + password1*`
  (no username, **no `password2`**). No `crispy-forms`, **no `django-widget-tweaks`**
  installed. Providers: Facebook (`method: js_sdk`) + Google. Signup is open.
- Templates under `templates/{account,socialaccount,openid}/` are **project
  overrides** (project `DIRS` is searched before app dirs).
- **Base-template chain:** `account/base.html` → `_base.html` (site shell). But
  the MVP pages (`login`, `signup`, `password_reset`, `password_reset_from_key`,
  `password_change`, `password_set`, `email`) **extend `_base.html` directly**,
  bypassing `account/base.html` (a one-line no-op pass-through). Only the notice
  pages (`account_inactive`, `signup_closed`, `verification_sent`,
  `verified_email_required`) extend `account/base.html`. → **There is no single
  injection point today**; a shared auth-card layout must be introduced (see
  Decision 1).
- `_base.html` loads `css/base.css` + `css/nav.css`, and renders the shared nav +
  footer on every auth page. New auth CSS should land in **`base.css`** (reusing
  tokens), not a new stylesheet.
- **Form fields are already hand-written per-field markup** —
  `<label class="stack"><span>Email</span>{{ form.login }}{{ form.login.errors }}</label>`
  — not `{{ form.as_p }}`. allauth's default widgets emit **no `class` attribute**;
  inputs get their look purely from the element selector `input, textarea, select`.
  → A CSS + light-class restyle is safe; **no widget-tweaks/crispy needed** (see
  Decision 2 rationale).

### What already exists to reuse (do not rebuild)
| Login-card piece | Shipped style | Verdict |
| --- | --- | --- |
| Card surface | `.box` — `#fff`, `1px solid var(--border)`, `border-radius: var(--border-radius)` (8px), `box-shadow: 0 1px 2px 0 rgb(0 0 0/.05)`, `padding: var(--s1)` | **reuse** |
| Labelled inputs | `input` element rule — 38px height, `1px solid var(--input)`, 8px radius, steel-blue focus ring (`box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 28%, transparent)`); `form label { font-weight: 700 }`; `.stack` gap | **reuse verbatim** |
| Primary submit | `.button` — black `--primary`, hover-lift + shadow, `:focus-visible` accent ring, `:active` press, disabled dim | **reuse (needs width — see gaps)** |
| Outline social button | `.button.outline` — white bg, `1px solid var(--input)`, hover `--muted` | **reuse (needs width; migrate off `.box.login`)** |
| "Forgot password?" | `a.secondaryAction { font-size: var(--s-1) }` + global accent `a` | **reuse** |
| Sign-up link | global `a` (steel-blue, underline on hover) | **reuse** |
| Social/email divider | `.or-separator` (flex hairlines + `<i>or</i>`) | **reuse (tokenize its greys — see gaps)** |
| Tokens | `--card`/`--card-foreground` (defined, **unused**), `--border`, `--input`, `--radius`, `--muted-foreground`, `--primary`/`--primary-foreground`, `--accent`/`--ring`, `--google-blue` | **all present** |

### The gaps (all tiny, static-CSS only)
1. **Full-width button variant.** `.button` (and `.button.outline`) are
   `display: inline-flex`, not full width. Add one rule — e.g.
   `.button.block { width: 100%; }` — applied to the submit and the social button.
2. **Social buttons still use the legacy `.box.login` box** (bordered box, text
   wrapped in `<h2>`, underline-on-hover). Migrate to `.button.outline.block` (all three classes on one element — `class="button outline block"`, not a descendant selector)
   for a real outline button matching basecoat (optionally with a brand icon).
3. **Card description subtitle** has no dedicated style. Reuse `.help-text` /
   `--muted-foreground`, or add one small card-scoped rule.
4. **`.or-separator` uses hardcoded greys** (`#6f6f6f`, `#cac7c7`). Retoken to
   `--muted-foreground` / `--border` — consistent with the design-system token
   drive.
5. **Optional card-width shell.** No ready-made centered "auth card" width wrapper;
   compose from `.center` + `max-width:measure/2`, or add a small `.auth-card` rule.

### The locked design language (carry the guardrails)
Faithful B&W · **black buttons** (the primary submit is `--primary` `#0a0a0a`,
**never** steel-blue) · **steel-blue `#31759d`** for accents only (links, focus
rings, soft tags) · **8px radius** · `system-ui` font · soft tags. **Do NOT use
`.card-box`** for the login card — its `a::after` full-card click overlay would
swallow clicks inside the form; use `.box`.

## Decisions to confirm before building

1. **Shared auth-card layout vs. per-page restyle.** *Recommended: introduce a
   shared card shell.* Make `account/base.html` (or a new
   `account/snippets/auth_card.html` include) the one place that renders the
   centered `.box` card + header slot, exposing blocks like `{% block auth_title %}`,
   `{% block auth_description %}`, `{% block auth_body %}`, `{% block auth_footer %}`;
   repoint the MVP pages to extend it. Single source of truth, so the card look is
   defined once. *Trade-off:* repointing changes each page's inheritance (small
   blast radius, easily guarded by template-render tests). *Lighter alternative:*
   leave each page on `_base.html` and just wrap its content in `.box` per-page —
   less refactor, but duplicates the card shell N times. Recommend the shared
   layout; fall back to per-page if the reset/from-key pages don't fit the shared
   header/footer cleanly.
2. **Social buttons: migrate `.box.login` → `.button.outline.block`** (the
   compound `class="button outline block"` on one element).
   *Recommended: yes*, with an optional inline brand SVG (Google G / Facebook f)
   and centered label. Keep **both** providers (basecoat shows one; we have two).
   Preserve `{% provider_login_url %}`, the Facebook `method="js_sdk"`, and
   `{% providers_media_js %}`.
3. **MVP scope / PR slicing** (below). Confirm the three-PR split or collapse
   A+B if the diffs stay small.

## PR breakdown

### PR A — Auth card shell + login page (the headline)
**Current state:** `account/login.html` extends `_base.html`; renders `<h1>Login</h1>`,
a centered "No account? sign up" `<p>`, `.box.login.facebook`/`.google` anchors,
`.or-separator`, then a `.stack-auth-form` email form with `.label.stack` fields,
`a.secondaryAction` (Forgot Password), and `<button class="primaryAction button">`.

**Scope:**
- Introduce the shared card shell (Decision 1) and render login inside it: card
  header (title + muted description + Sign-Up link), body (email + password fields,
  Forgot-password inline to the right of the Password label), footer (full-width
  submit, then full-width outline Google + Facebook).
- Add the gap CSS to `base.css` under an "auth card" section comment:
  `.button.block { width: 100%; }`, the card description rule, the retokenized
  `.or-separator`, and (if used) `.auth-card` width.
- Migrate the social anchors to `.button.outline.block` (Decision 2).
- **Preserve exactly:** `{% csrf_token %}`, `{{ form.non_field_errors }}` + each
  `{{ form.<field>.errors }}`, the `redirect_field_value` hidden input,
  `for="{{ form.<field>.id_for_label }}"` label association, and
  `{% providers_media_js %}` + the Facebook `js_sdk` method. Keep interpolating
  `{{ form.login }}` / `{{ form.password }}` (login field is named `login`, not
  `email`).

**Red-green tests** (`app/store_project/pages/tests/test_design_system_phase3.py`):
- *CSS guard* (`SimpleTestCase`, `_css_block`): `.button.block { … width: 100% … }`
  exists; `.or-separator` block references `var(--muted-foreground)` / `var(--border)`
  and no longer contains `#6f6f6f` / `#cac7c7`.
- *Template guard* (`TestCase`, `client.get(reverse("account_login"))`,
  `assertContains`/`assertNotContains`): the login page renders the card
  (`class="box"` wrapper) and full-width buttons (`class="button outline block"` or
  the chosen classes), and **no longer** renders the old `class="box login google"`
  box treatment. Assert the shared nav (`class="nav"`) still present.

### PR B — Signup + password-reset twins
**Current state:** `account/signup.html` mirrors login (with a stray, copy-pasted
"Forgot Password?" link to drop). `account/password_reset.html` uses an odd
`.form-with-sidebar` layout; `account/password_reset_from_key.html` is a bare
`.stack` form with label/error ordering quirks.

**Scope:** apply the same card shell. Signup: same header/body/footer, drop the
stray forgot-password link (signup has **no `password2`** — one password field
only). Reconcile `password_reset` and `password_reset_from_key` onto the card
(simpler bodies: one email field, or two password fields) so the whole flow reads
as one card family.

**Red-green tests:** template guards for `account_signup` (card present, single
password field, stray forgot-link gone) and the two reset templates; a CSS guard
only if new rules are added (reuse PR A's).

### PR C — Second-tier auth pages
**Scope:** bring `password_change`, `password_set`, `email` (manage addresses),
the confirmation pages (`password_reset_done`, `password_reset_from_key_done`,
`email_confirm`), and the `socialaccount/*` confirm/connections pages into the same
card family. The `socialaccount/*` pages use allauth's `{% element %}`/`{% slot %}`
DSL — restyle those mostly via CSS on `.primaryAction.button` / `.socialaccount_provider`
rather than markup surgery. **Notice pages** (`account_inactive`, `signup_closed`,
`verification_sent`, `verified_email_required`) extend `account/base.html` **and
define their own `{% block content %}`** — so if the shared card shell lives in
`account/base.html`'s `content` block (Decision 1), their child blocks *override*
it and they will **not** pick up the card automatically. Migrate each notice page
to the new auth blocks (e.g. `{% block auth_body %}`) explicitly, or leave them on
the plain shell — don't assume inheritance covers them.

**Red-green tests:** lightweight template guards per page (including a guard that
each notice page actually renders the card wrapper, since inheritance won't); reuse
PR A/B CSS.

## Red-green test strategy (the idiom to copy)

Place all guards in `app/store_project/pages/tests/test_design_system_phase3.py`
(the established home for these suites; pytest picks up `test_*.py`). Two idioms,
both already used in `test_design_system_phase2.py`:

**CSS guards** — read the real source stylesheet, assert on the parsed rule block
(red on `main`, green after the source edit):
```python
from pathlib import Path
from django.test import SimpleTestCase

APP_ROOT = Path(__file__).resolve().parents[2]   # -> app/store_project
BASE_CSS = APP_ROOT / "static" / "css" / "base.css"

def _css_block(css: str, selector: str) -> str:
    start = css.index(selector); brace = css.index("{", start); end = css.index("}", brace)
    return css[brace : end + 1]

class Phase3CssTests(SimpleTestCase):
    def test_button_block_is_full_width(self):
        block = _css_block(BASE_CSS.read_text(), "\n.button.block {")  # \n anchors line-start
        self.assertIn("width: 100%", block)
```
(`SimpleTestCase` — no DB; anchor selectors with a leading `\n` to force
line-start matching, as the phase-2 suite does.)

**Template guards** — render the real auth page and assert markup:
```python
from django.test import TestCase
from django.urls import reverse

class Phase3LoginTemplateTests(TestCase):
    def test_login_uses_the_card(self):
        resp = self.client.get(reverse("account_login"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="box"')
        self.assertNotContains(resp, 'class="box login google"')
```
(`TestCase`/`django_db` because allauth touches the session.) URL names:
`account_login`, `account_signup`, `account_reset_password`. Run a single file with
`uv run pytest app/store_project/pages/tests/test_design_system_phase3.py`; full
suite via `just test` (settings `config.settings.test`).

## Risks
- **Blast radius is lower than phase 2's** (auth pages only, not every page), but
  repointing template inheritance (Decision 1) can silently drop a block — cover
  each MVP page with a render guard.
- **allauth field wiring is dynamic.** Keep interpolating the exact field names
  (`login`, `password`, `email`, `password1`) and preserve `{% csrf_token %}`,
  error blocks, hidden redirect input, and label `for=` associations. A CSS-only
  restyle can't break this; a form rewrite could — so we don't rewrite forms.
- **Social JS.** `{% providers_media_js %}` and the Facebook `js_sdk` method must
  survive the social-button migration or the popup login breaks.
- **`.box.login` reuse.** It's also referenced by `account/snippets/login_box.html`
  (Google-only embed) — grep for its includes before removing the old rule; migrate
  or leave the rule until the snippet is updated.

## Verification
- `just test` (incl. the new phase-3 guards) + `just lint` + `pre-commit`
  (djhtml + biome) green before pushing.
- After CSS edits, `uv run python app/manage.py collectstatic --noinput` so
  WhiteNoise serves the change on the dev server (port 8034; seed store with
  `seed_products`).
- Browser before/after on **login, signup, password-reset request, password-reset
  from-key, and one second-tier page (email management)** — desktop + mobile.
  Confirm the steel-blue focus ring, black submit, full-width outline social
  button, and that the shared nav/footer still frame the card.
- Codex review loop; ship through the human merge/deploy gate (mastering.fitness).
  Template/CSS changes are **not** docs-only, so this ships through the normal
  Django CI → Deploy pipeline (unlike this plan doc).

## Conventions (carry over from phases 1–2)
- Red→green tests that read the real CSS / render real templates.
- Reuse `:root` tokens; never reintroduce hardcoded hexes (retoken `.or-separator`).
- Keep the primary button **black**; steel-blue is accents only.
- `just test` + `just lint` + `pre-commit` before pushing; `collectstatic` for dev.
- Ship through the human merge/deploy gate; merge to `main` auto-deploys via
  GH Actions (Django CI → Deploy).

## Explicitly out of scope
- Base Coat-proper (Tailwind + Node build) — the separate larger initiative the
  phase-2 plan deferred.
- Any allauth **form** subclassing / `crispy-forms` / `widget-tweaks` (none
  installed; not needed for a CSS restyle).
- `openid/login.html` (no OpenID provider configured — dead), all `*/email/*.txt`
  and `*/messages/*.txt` (plain-text, non-HTML).
- Changing auth behaviour, providers, or copy beyond what the card layout requires.
