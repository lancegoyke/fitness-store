/* Meso — warn before navigating away from a form with unsaved edits (#646).
 * Any <form data-dirty-guard> snapshots its controls on load (form.elements
 * includes controls attached from elsewhere via the form="..." attribute) and
 * arms a beforeunload prompt only while a value differs from that snapshot.
 * Submitting the form itself disarms it.
 */
(function (root) {
  "use strict";

  function controlValue(el) {
    if (el.type === "checkbox" || el.type === "radio") {
      return el.checked ? "1" : "0";
    }
    return el.value;
  }

  function trackable(el) {
    if (!el.name) return false;
    return !["submit", "button", "reset", "image", "file", "hidden"].includes(
      el.type
    );
  }

  function snapshot(form) {
    const values = new Map();
    Array.from(form.elements).forEach((el) => {
      if (trackable(el)) values.set(el, controlValue(el));
    });
    return values;
  }

  function isDirty(form, initial) {
    return Array.from(form.elements).some(
      (el) => trackable(el) && initial.get(el) !== controlValue(el)
    );
  }

  function guard(form, win) {
    let initial = snapshot(form);
    let submitting = false;
    form.addEventListener("submit", () => {
      submitting = true;
    });
    // An aborted submit or a bfcache restore must not leave the guard off; a
    // restored page also re-baselines on whatever the browser put back.
    win.addEventListener("pageshow", (event) => {
      submitting = false;
      if (event.persisted) initial = snapshot(form);
    });
    win.addEventListener("beforeunload", (event) => {
      if (submitting || !isDirty(form, initial)) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  function init() {
    const doc = root.document;
    if (!doc) return;
    doc.querySelectorAll("form[data-dirty-guard]").forEach((form) => {
      guard(form, root);
    });
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
    module.exports = { snapshot, isDirty, guard };
  }
})(typeof window !== "undefined" ? window : this);
