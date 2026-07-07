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
 * parsing, per-step action state, current-page detection) is unit-tested
 * (frontend/meso_tour.test.js); the DOM wiring below is exercised through the
 * Django template/view tests.
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
    var showGoto = !isCurrentPage(step.url, root.location.pathname);
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

    return (
      '<div class="meso-tour-spotlight" data-tour-spotlight aria-hidden="true"></div>' +
      '<div class="meso-card meso-card--pad meso-tour-card' +
      (step.anchor ? "" : " meso-tour-card--centered") +
      '" role="dialog" aria-label="Guided tour" tabindex="-1">' +
      '<button type="button" class="meso-tour-close" data-tour-dismiss aria-label="Dismiss tour">×</button>' +
      '<p class="meso-eyebrow">Step ' +
      (index + 1) +
      " of " +
      total +
      "</p>" +
      '<div class="meso-tour-title">' +
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
      '<form method="post" action="' +
      escapeHtml(config.skip_url) +
      '" class="meso-tour-skip-form">' +
      '<input type="hidden" name="csrfmiddlewaretoken" value="' +
      csrf +
      '">' +
      '<button type="submit" class="meso-tour-skip">Skip tour · load everything</button>' +
      "</form>" +
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
    var pad = 6;
    spotlight.style.display = "block";
    spotlight.style.top = Math.max(rect.top - pad, 0) + "px";
    spotlight.style.left = Math.max(rect.left - pad, 0) + "px";
    spotlight.style.width = rect.width + pad * 2 + "px";
    spotlight.style.height = rect.height + pad * 2 + "px";
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
      positionSpotlight(mount, config.steps[index].anchor, 0);
      wireCard();
      var card = mount.querySelector(".meso-tour-card");
      if (card && card.focus) card.focus();
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
      }
      doc.removeEventListener("keydown", onKeydown);
      mount.innerHTML = "";
    }

    reposition = throttle(function () {
      positionSpotlight(mount, config.steps[index].anchor, 0);
    }, 100);
    win.addEventListener("resize", reposition);
    win.addEventListener("scroll", reposition, true);
    doc.addEventListener("keydown", onKeydown);

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
    };
  }
})(typeof window !== "undefined" ? window : this);
