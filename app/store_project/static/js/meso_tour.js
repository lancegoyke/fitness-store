/* Meso — guided demo onboarding tour driver (issue #430, Phase 2 sandbox +
 * Phase 3 real-coach self-coaching variant).
 *
 * Hand-rolled vanilla JS (no Alpine, no CDN — mirrors meso_onboarding.js's
 * style): reads its config from a `json_script` element `_tour.html` embeds
 * (`#meso-tour-config`), then renders a spotlight + card overlay into the
 * server-rendered mount (`#meso-tour`) and drives it — Next/Back/dismiss
 * persist server-side via `fetch` to `state_url`; the per-step data action
 * (sandbox: "add sample data" against a `segment`; self-coaching: a typed
 * `action` — `roster_add_self`/`plan_create` — Phase 3) and the "skip · load
 * everything" shortcut are real HTML `<form method="post">`s (a full page
 * reload, so Django's flash message + the freshly-true `loaded` flag show up
 * the normal way).
 *
 * The pure decision logic (step clamping/advance, anchor-retry cutoff, config
 * parsing, per-step action state, current-page detection, the a11y
 * announcement string, reduced-motion scroll behavior — Phase 4) is
 * unit-tested (frontend/meso_tour.test.js); the DOM wiring below is exercised
 * through the Django template/view tests.
 *
 * Mobile (issue #430 Phase 4): the step card becomes a bottom sheet on
 * narrow viewports via a plain CSS media query in `_tour.html`'s inline
 * `<style>` block, not a JS branch here — there's no viewport-width decision
 * to unit-test because the browser makes it.
 */
(function (root) {
  // ---- pure logic (unit-tested) ----

  // Clamp `step` into a valid index for `count` steps (never negative, never
  // past the last step). A non-numeric `step` (missing/garbled config) is
  // treated as step 0.
  function clampStep(step, count) {
    var n = typeof step === "number" ? step : parseInt(step, 10);
    if (!isFinite(n) || isNaN(n)) n = 0;
    if (count <= 0) return 0;
    return Math.max(0, Math.min(n, count - 1));
  }

  function nextIndex(step, count) {
    return clampStep(clampStep(step, count) + 1, count);
  }

  function prevIndex(step, count) {
    return clampStep(clampStep(step, count) - 1, count);
  }

  function isLastStep(step, count) {
    return clampStep(step, count) >= count - 1;
  }

  // Parse the tour config out of the `json_script` element's raw text.
  // Defensive: a missing element, empty text, malformed JSON, or a shape
  // without a non-empty `steps` array all just mean "no tour" (null) rather
  // than throwing — the caller no-ops in every one of those cases, exactly
  // like a real coach's page where `_tour.html` never even renders the mount.
  function parseTourConfig(rawText) {
    if (!rawText) return null;
    var config;
    try {
      config = JSON.parse(rawText);
    } catch (e) {
      return null;
    }
    if (!config || !Array.isArray(config.steps) || config.steps.length === 0) {
      return null;
    }
    return config;
  }

  // Whether the anchor-lookup retry loop should try again. Kept as a pure,
  // one-line decision so the retry cutoff (roughly 2s at the driver's poll
  // interval) is unit-testable without faking timers.
  function shouldRetryAnchor(attempt, maxAttempts) {
    return attempt < maxAttempts;
  }

  // What the step's action control should look like, given its `segment`
  // (sandbox data-loading action) / `action` (Phase 3 self-variant typed
  // form action — `{url, label, fields}`) / `signup_gate` (sandbox
  // agent+finish steps) / `loaded` (O7 — derived, never stored) fields.
  // Returns null for a step with no action at all (e.g. the "profile" step).
  // `segment` and `action` are mutually exclusive in practice (sandbox steps
  // carry the former, self-variant steps the latter), checked in that order.
  function resolveActionState(step) {
    if (!step) return null;
    if (step.segment) {
      return step.loaded
        ? { kind: "segment", label: "Added ✓", disabled: true }
        : {
            kind: "segment",
            label: step.action_label || "Add sample data",
            disabled: false,
          };
    }
    if (step.action) {
      return step.loaded
        ? { kind: "form", label: "Done ✓", disabled: true }
        : {
            kind: "form",
            label: step.action.label || "Continue",
            disabled: false,
          };
    }
    if (step.signup_gate) {
      return { kind: "signup", label: "Create a free account", disabled: false };
    }
    return null;
  }

  // Whether `stepUrl` (an absolute or relative URL) targets the page the
  // browser is already on — decides whether to show "Take me there". Step
  // URLs are the bare entry points (`/meso/designer/`, `/meso/deliver/`),
  // but those views redirect to an id-suffixed page (`/meso/designer/107/`)
  // — an exact-only match would keep offering "Take me there" on the very
  // page the step points at. So a step also matches any *subpath* of its
  // URL — except the app root itself (`/meso/`, the roster step), which
  // stays exact-match only: every meso path starts with `/meso/`, so the
  // prefix rule there would swallow every page.
  function isCurrentPage(stepUrl, currentPath) {
    if (!stepUrl) return true;
    var APP_ROOT = "/meso";
    function normalize(p) {
      return (p || "").replace(/\/+$/, "");
    }
    var target;
    try {
      target = new URL(stepUrl, "http://meso.invalid").pathname;
    } catch (e) {
      target = stepUrl;
    }
    target = normalize(target);
    var current = normalize(currentPath);
    if (current === target) return true;
    return (
      target.length > APP_ROOT.length && current.indexOf(target + "/") === 0
    );
  }

  // #441 P1-1: the sandbox-only "load everything" skip must never render for
  // a real (self-variant) coach — it would drop 5 fake demo athletes onto
  // their live roster. Hidden whenever the variant isn't explicitly sandbox.
  function shouldShowSkipLoad(config) {
    return !!config && config.variant === "sandbox";
  }

  // #441 P1-3: show "Take me there" only when the browser isn't already on
  // the step's page AND the server says the target will actually render — a
  // step whose data doesn't exist yet redirects to the roster, an infinite
  // bounce loop. A missing `goto_ready` means "no prerequisite" (the
  // roster-targeted steps), so it defaults to ready.
  function shouldShowGoto(step, currentPath) {
    if (!step) return false;
    if (step.goto_ready === false) return false;
    return !isCurrentPage(step.url, currentPath);
  }

  // #441 P1-4: a zero-area rect means the anchor is hidden/collapsed (e.g.
  // the deliver step's control inside Alpine's `x-show="!delivered"` once a
  // delivery is sent). Treat it as "anchor gone" so the driver hides the
  // spotlight instead of drawing a 12x12 hole at the viewport origin.
  function isUsableRect(rect) {
    return !!rect && (rect.width > 0 || rect.height > 0);
  }

  // The card's accessible name (issue #430 Phase 4, a11y): `aria-label` on
  // the dialog and the text of the `aria-live` announcement on step change
  // both read "Step 3 of 8: Program Designer" — the step position plus its
  // title, so a screen-reader user always knows both where they are and what
  // this step is about without having to find the visible "Step X of Y" text.
  function buildStepAnnouncement(step, index, total) {
    var title = (step && step.title) || "";
    return "Step " + (index + 1) + " of " + total + (title ? ": " + title : "");
  }

  // Whether the visitor's OS/browser asks for reduced motion — a plain
  // boolean read from a `window`-like object rather than the global `window`
  // directly, so it's callable with a fake `{ matchMedia: ... }` in tests.
  function prefersReducedMotion(win) {
    return !!(
      win &&
      typeof win.matchMedia === "function" &&
      win.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  // The `scrollIntoView`/scroll behavior to use, given whether reduced motion
  // is requested (issue #430 Phase 4): instant ("auto") under reduced motion,
  // smooth otherwise.
  function scrollBehaviorFor(reducedMotion) {
    return reducedMotion ? "auto" : "smooth";
  }

  // #441 P3-1: the step card is a bottom sheet on narrow viewports (the
  // `@media (max-width:640px)` block in `_tour.html`, height `min(75vh,520px)`
  // pinned `bottom:0`), which covers viewport center and buries a
  // `block:"center"` scrolled anchor behind it. This returns the sheet's height
  // in px so `scrollAnchorIntoView` can lift the anchor's resting position out
  // from under it — 0 off the sheet layout (or when `matchMedia`/`innerHeight`
  // is missing), so a rotate-to-desktop leaves no stale margin. A plain px value
  // (not the `min(75vh,520px)` CSS string) so CSSOM accepts it as-is.
  function bottomSheetInset(win) {
    if (!win || typeof win.matchMedia !== "function") return 0;
    if (typeof win.innerHeight !== "number") return 0;
    if (!win.matchMedia("(max-width: 640px)").matches) return 0;
    return Math.round(Math.min(win.innerHeight * 0.75, 520));
  }

  // ---- DOM wiring (browser only) ----

  var MAX_ANCHOR_ATTEMPTS = 10;
  var ANCHOR_RETRY_MS = 200;

  function getCookie(name) {
    var pattern = new RegExp(
      "(?:^|; )" + name.replace(/([.$?*|{}()[\]\\/+^])/g, "\\$1") + "=([^;]*)"
    );
    var match = root.document.cookie.match(pattern);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[
          c
        ] || c
      );
    });
  }

  function postState(stateUrl, action, extra) {
    var params = new URLSearchParams({ action: action });
    if (extra) {
      Object.keys(extra).forEach(function (key) {
        params.set(key, extra[key]);
      });
    }
    return root
      .fetch(stateUrl, {
        method: "POST",
        headers: {
          "X-CSRFToken": getCookie("csrftoken") || "",
          "X-Requested-With": "XMLHttpRequest",
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: params.toString(),
      })
      .catch(function () {
        /* best-effort — the local step already advanced; a lost network call
           just means the next page load resumes from the last-saved step */
      });
  }

  function throttle(fn, wait) {
    var last = 0;
    var timer = null;
    return function () {
      var now = Date.now();
      var remaining = wait - (now - last);
      if (remaining <= 0) {
        last = now;
        fn();
      } else if (!timer) {
        timer = root.setTimeout(function () {
          last = Date.now();
          timer = null;
          fn();
        }, remaining);
      }
    };
  }

  function buildCardMarkup(config, index) {
    var step = config.steps[index];
    var total = config.steps.length;
    var action = resolveActionState(step);
    var showGoto = shouldShowGoto(step, root.location.pathname);
    var csrf = escapeHtml(getCookie("csrftoken") || "");

    var dots = config.steps
      .map(function (_s, i) {
        return '<span class="' + (i === index ? "is-active" : "") + '"></span>';
      })
      .join("");

    var actionHtml = "";
    if (action && action.kind === "segment") {
      // `next` sends demo_load back to this very page after the load (the
      // view validates it as a safe local path) — without it every segment
      // action would land on the roster, teleporting the user away from the
      // mid-tour page (designer, deliver, ...) the step lives on.
      actionHtml =
        '<form method="post" action="' +
        escapeHtml(config.demo_load_url) +
        '" class="meso-tour-action-form">' +
        '<input type="hidden" name="csrfmiddlewaretoken" value="' +
        csrf +
        '">' +
        '<input type="hidden" name="segment" value="' +
        escapeHtml(step.segment) +
        '">' +
        '<input type="hidden" name="next" value="' +
        escapeHtml(root.location.pathname + root.location.search) +
        '">' +
        '<button type="submit" class="meso-btn meso-btn--primary"' +
        (action.disabled ? " disabled" : "") +
        ">" +
        escapeHtml(action.label) +
        "</button></form>";
    } else if (action && action.kind === "form") {
      // Self-variant typed action (Phase 3): a real form POST straight to the
      // resolved endpoint (`roster_add_self` / `plan_create`) with whatever
      // hidden fields the step specifies (e.g. `draft=agent`). No `next` field
      // — unlike the sandbox's segment action, these endpoints already
      // redirect to the right place (roster / the designer), which the plan
      // deliberately leans on rather than fighting.
      //
      // `tour=1` (Phase 4 analytics): both endpoints are also hit organically
      // (roster.html's own "Add yourself" row, every "+ New program" CTA) —
      // this marker is how the server tells a tour-driven opt-in apart from
      // an organic one, so only forms *this driver* builds carry it.
      var fields = (step.action && step.action.fields) || {};
      var fieldsHtml = Object.keys(fields)
        .map(function (name) {
          return (
            '<input type="hidden" name="' +
            escapeHtml(name) +
            '" value="' +
            escapeHtml(fields[name]) +
            '">'
          );
        })
        .join("");
      actionHtml =
        '<form method="post" action="' +
        escapeHtml(step.action.url) +
        '" class="meso-tour-action-form">' +
        '<input type="hidden" name="csrfmiddlewaretoken" value="' +
        csrf +
        '">' +
        '<input type="hidden" name="tour" value="1">' +
        fieldsHtml +
        '<button type="submit" class="meso-btn meso-btn--primary"' +
        (action.disabled ? " disabled" : "") +
        ">" +
        escapeHtml(action.label) +
        "</button></form>";
    } else if (action && action.kind === "signup") {
      actionHtml =
        '<a class="meso-btn meso-btn--primary" href="' +
        escapeHtml(config.signup_url) +
        '">' +
        escapeHtml(action.label) +
        "</a>";
    }

    var gotoHtml = showGoto
      ? '<a class="meso-tour-goto" href="' +
        escapeHtml(step.url) +
        '">Take me there →</a>'
      : "";

    // `aria-label` carries the step position + title (issue #430 Phase 4 a11y)
    // rather than `aria-modal` — the page behind the card must stay reachable
    // (the whole point of a spotlight tour is clicking the highlighted live
    // control), so this deliberately isn't a true modal dialog.
    var ariaLabel = escapeHtml(buildStepAnnouncement(step, index, total));

    return (
      '<div class="meso-tour-spotlight" data-tour-spotlight aria-hidden="true"></div>' +
      '<div class="meso-card meso-card--pad meso-tour-card' +
      (step.anchor ? "" : " meso-tour-card--centered") +
      '" role="dialog" aria-label="' +
      ariaLabel +
      '">' +
      '<button type="button" class="meso-tour-close" data-tour-dismiss aria-label="Dismiss tour">×</button>' +
      '<p class="meso-eyebrow">Step ' +
      (index + 1) +
      " of " +
      total +
      "</p>" +
      '<div class="meso-tour-title" tabindex="-1" data-tour-heading>' +
      escapeHtml(step.title) +
      "</div>" +
      '<p class="meso-sub meso-tour-bodytext">' +
      escapeHtml(step.body) +
      "</p>" +
      '<div class="meso-tour-dots">' +
      dots +
      "</div>" +
      (actionHtml || gotoHtml
        ? '<div class="meso-tour-actions">' + actionHtml + gotoHtml + "</div>"
        : "") +
      '<div class="meso-tour-nav">' +
      '<button type="button" class="meso-btn meso-btn--ghost" data-tour-back' +
      (index === 0 ? " disabled" : "") +
      ">Back</button>" +
      '<button type="button" class="meso-btn meso-btn--primary" data-tour-next>' +
      (isLastStep(index, total) ? "Finish" : "Next") +
      "</button>" +
      "</div>" +
      (shouldShowSkipLoad(config)
        ? '<form method="post" action="' +
          escapeHtml(config.skip_url) +
          '" class="meso-tour-skip-form">' +
          '<input type="hidden" name="csrfmiddlewaretoken" value="' +
          csrf +
          '">' +
          '<button type="submit" class="meso-tour-skip">Skip tour · load everything</button>' +
          "</form>"
        : "") +
      "</div>"
    );
  }

  function positionSpotlight(mount, anchorValue, attempt) {
    var spotlight = mount.querySelector("[data-tour-spotlight]");
    if (!spotlight) return;
    if (!anchorValue) {
      spotlight.style.display = "none";
      return;
    }
    var selector = '[data-tour="' + anchorValue.replace(/"/g, '\\"') + '"]';
    var el = root.document.querySelector(selector);
    if (!el) {
      if (shouldRetryAnchor(attempt, MAX_ANCHOR_ATTEMPTS)) {
        root.setTimeout(function () {
          positionSpotlight(mount, anchorValue, attempt + 1);
        }, ANCHOR_RETRY_MS);
      } else {
        spotlight.style.display = "none";
      }
      return;
    }
    var rect = el.getBoundingClientRect();
    if (!isUsableRect(rect)) {
      spotlight.style.display = "none";
      return;
    }
    var pad = 6;
    spotlight.style.display = "block";
    spotlight.style.top = Math.max(rect.top - pad, 0) + "px";
    spotlight.style.left = Math.max(rect.left - pad, 0) + "px";
    spotlight.style.width = rect.width + pad * 2 + "px";
    spotlight.style.height = rect.height + pad * 2 + "px";
  }

  // Brings the step's spotlighted anchor into view on step change (issue
  // #430 Phase 4) — a bottom-sheet card on mobile, or a page that's scrolled
  // away from the control, would otherwise spotlight something off-screen.
  // A one-shot best-effort lookup (no retry loop like `positionSpotlight`'s —
  // this only runs once per step change, not on every resize/scroll, so
  // missing an anchor that mounts asynchronously just means no auto-scroll
  // for that step; the spotlight box itself still finds it via its own
  // retries). `prefers-reduced-motion` picks an instant jump over a smooth
  // scroll.
  function scrollAnchorIntoView(doc, win, anchorValue) {
    if (!anchorValue) return;
    var selector = '[data-tour="' + anchorValue.replace(/"/g, '\\"') + '"]';
    var el = doc.querySelector(selector);
    if (!el || typeof el.scrollIntoView !== "function") return;
    // #441 P3-1: lift the anchor's resting position out from behind the mobile
    // bottom sheet before scrolling (with `block:"center"` a `scroll-margin-
    // bottom` of S raises the anchor by ~S/2). Cleared to "" off the sheet
    // layout so a rotate-to-desktop leaves no stale margin.
    var inset = bottomSheetInset(win);
    el.style.scrollMarginBottom = inset ? inset + "px" : "";
    el.scrollIntoView({
      behavior: scrollBehaviorFor(prefersReducedMotion(win)),
      block: "center",
    });
  }

  // Wires one render of the card: back/next/dismiss buttons. The per-step
  // action and skip forms, and the "Take me there" link, are real
  // form/anchor markup (a genuine page navigation) — no JS wiring needed.
  function initTour(doc, win) {
    var mount = doc.getElementById("meso-tour");
    var configEl = doc.getElementById("meso-tour-config");
    if (!mount || !configEl) return;
    var config = parseTourConfig(configEl.textContent);
    if (!config) return;

    var index = clampStep(config.step, config.steps.length);
    var reposition = null;

    // A persistent `aria-live` region (issue #430 Phase 4 a11y) — created
    // once and only ever text-updated, never recreated, unlike the card
    // itself (`render()` replaces `mount`'s entire subtree each step via
    // `innerHTML`). Screen readers reliably announce a *mutation* to an
    // existing live region; inserting a brand-new node that already has
    // `aria-live` set is announced less consistently across engines, so this
    // lives outside `mount` entirely and just has its text swapped.
    var liveRegion = doc.createElement("div");
    liveRegion.className = "meso-tour-sr-only";
    liveRegion.setAttribute("aria-live", "polite");
    liveRegion.setAttribute("role", "status");
    doc.body.appendChild(liveRegion);

    function wireCard() {
      var back = mount.querySelector("[data-tour-back]");
      var next = mount.querySelector("[data-tour-next]");
      var dismiss = mount.querySelector("[data-tour-dismiss]");
      if (back) {
        back.addEventListener("click", function () {
          goTo(prevIndex(index, config.steps.length));
        });
      }
      if (next) {
        next.addEventListener("click", handleNext);
      }
      if (dismiss) {
        dismiss.addEventListener("click", function () {
          postState(config.state_url, "dismiss").then(teardown);
        });
      }
    }

    function render() {
      mount.innerHTML = buildCardMarkup(config, index);
      // The mount starts `aria-hidden="true"` in `_tour.html` (nothing to
      // read before JS ever mounts a card) — flip it open now that it holds
      // real, interactive content, or a screen reader can never reach it at
      // all despite it being keyboard-focusable (an axe-flagged anti-pattern:
      // aria-hidden on a focusable element/ancestor).
      mount.setAttribute("aria-hidden", "false");
      positionSpotlight(mount, config.steps[index].anchor, 0);
      scrollAnchorIntoView(doc, win, config.steps[index].anchor);
      wireCard();
      // Focus the heading, not the whole card (issue #430 Phase 4 a11y) —
      // `preventScroll` so moving focus never fights the anchor scroll above
      // or jerks the page around on every step.
      var heading = mount.querySelector("[data-tour-heading]");
      if (heading && heading.focus) heading.focus({ preventScroll: true });
      liveRegion.textContent = buildStepAnnouncement(
        config.steps[index],
        index,
        config.steps.length
      );
    }

    function goTo(newIndex) {
      index = clampStep(newIndex, config.steps.length);
      postState(config.state_url, "goto", { step: index });
      render();
    }

    function handleNext() {
      if (isLastStep(index, config.steps.length)) {
        postState(config.state_url, "complete").then(teardown);
      } else {
        goTo(nextIndex(index, config.steps.length));
      }
    }

    function onKeydown(e) {
      if (e.key === "Escape") {
        postState(config.state_url, "dismiss").then(teardown);
      } else if (e.key === "ArrowRight") {
        handleNext();
      } else if (e.key === "ArrowLeft") {
        goTo(prevIndex(index, config.steps.length));
      }
    }

    function teardown() {
      if (reposition) {
        win.removeEventListener("resize", reposition);
        win.removeEventListener("scroll", reposition, true);
        doc.removeEventListener("toggle", reposition, true);
      }
      doc.removeEventListener("keydown", onKeydown);
      doc.removeEventListener("meso:tour-refresh", onTourRefresh);
      mount.innerHTML = "";
      mount.setAttribute("aria-hidden", "true");
      if (liveRegion.parentNode) liveRegion.parentNode.removeChild(liveRegion);
    }

    // #451: the self-variant deliver/results steps take their real,
    // data-producing action via a `fetch` (deliver the coach's own block / log
    // their own session) with no page reload — so the server advances
    // `tour_state` (`advance_self_step_if_complete` in
    // `plan_deliver`/`athlete_log_session`) but this already-mounted card keeps
    // its local `index` until the next navigation. `meso_deliver.js` /
    // `meso_athlete.js` dispatch a `meso:tour-refresh` document event after a
    // successful fetch action; on it we re-read the authoritative config from
    // the read-only `config_url` and re-render at whatever step the server now
    // reports. Advance stays server-authoritative — the `TourEvent` funnel and
    // the resume step both live in `tour_state`, written only at the tour's own
    // POST endpoints — so this is a DISPLAY update only and MUST NOT `postState`
    // (the server already advanced; posting would double-count/fight it).
    function onTourRefresh() {
      // Older configs (served before this endpoint existed) carry no
      // `config_url` — nothing to re-read, so leave the card exactly as it is.
      if (!config.config_url) return;
      root
        .fetch(config.config_url, {
          headers: { "X-Requested-With": "XMLHttpRequest" },
        })
        .then(function (res) {
          return res.json();
        })
        .then(function (fresh) {
          // A steps-less / empty snapshot means "no tour" (e.g. no
          // CoachProfile) — nothing to show, so don't disturb the card.
          if (
            !fresh ||
            !Array.isArray(fresh.steps) ||
            fresh.steps.length === 0
          ) {
            return;
          }
          // The tour ended out from under us (the coach finished or dismissed
          // it elsewhere) — tear down rather than re-render a dead tour.
          if (fresh.status === "dismissed" || fresh.status === "completed") {
            teardown();
            return;
          }
          // Adopt the fresh config + step and re-render. `render`/`goTo`/the
          // `reposition` throttle all close over these same `config`/`index`
          // vars (declared with `var` above), so reassigning them here updates
          // every subsequent read — a display mirror of the server's state.
          config = fresh;
          index = clampStep(fresh.step, fresh.steps.length);
          render();
        })
        .catch(function () {
          /* best-effort — a lost read just leaves the card on its current step;
             the coach's next navigation resumes from the last-saved step */
        });
    }

    reposition = throttle(function () {
      positionSpotlight(mount, config.steps[index].anchor, 0);
    }, 100);
    win.addEventListener("resize", reposition);
    win.addEventListener("scroll", reposition, true);
    // #441 P3-3: a spotlighted `<details>` (step 8's roster-invite) fires a
    // non-bubbling `toggle` on expand — listen in capture so the spotlight
    // re-measures instead of going stale at the collapsed size.
    doc.addEventListener("toggle", reposition, true);
    doc.addEventListener("keydown", onKeydown);
    // #451: re-read the server's authoritative step after a fetch action that
    // auto-advanced the tour without a page reload (self deliver/results).
    doc.addEventListener("meso:tour-refresh", onTourRefresh);

    render();
  }

  function init() {
    var doc = root.document;
    if (!doc) return;
    initTour(doc, root);
  }

  if (typeof document !== "undefined" && document.addEventListener) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }

  // Test hook for Node-based runners (vitest); skipped in the browser.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      clampStep: clampStep,
      nextIndex: nextIndex,
      prevIndex: prevIndex,
      isLastStep: isLastStep,
      parseTourConfig: parseTourConfig,
      shouldRetryAnchor: shouldRetryAnchor,
      resolveActionState: resolveActionState,
      isCurrentPage: isCurrentPage,
      buildStepAnnouncement: buildStepAnnouncement,
      prefersReducedMotion: prefersReducedMotion,
      scrollBehaviorFor: scrollBehaviorFor,
      shouldShowSkipLoad: shouldShowSkipLoad,
      shouldShowGoto: shouldShowGoto,
      isUsableRect: isUsableRect,
      bottomSheetInset: bottomSheetInset,
      scrollAnchorIntoView: scrollAnchorIntoView,
      initTour: initTour,
    };
  }
})(typeof window !== "undefined" ? window : this);
