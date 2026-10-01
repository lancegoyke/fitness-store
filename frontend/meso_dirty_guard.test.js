// Tests for the unsaved-changes guard
// (app/store_project/static/js/meso_dirty_guard.js).

import { snapshot, isDirty, guard } from "../app/store_project/static/js/meso_dirty_guard.js";

function build() {
  document.body.innerHTML = `
    <form id="f" data-dirty-guard method="post">
      <input type="text" name="a" value="one" />
      <button type="submit" name="go">Go</button>
    </form>
    <textarea name="b" form="f">two</textarea>
    <select name="c" form="f"><option value="x" selected>x</option><option value="y">y</option></select>`;
  return document.getElementById("f");
}

function fireUnload() {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event;
}

describe("dirty guard", () => {
  it("is clean until a value changes, and clean again when reverted", () => {
    const form = build();
    const initial = snapshot(form);
    expect(isDirty(form, initial)).toBe(false);
    form.elements.namedItem("a").value = "changed";
    expect(isDirty(form, initial)).toBe(true);
    form.elements.namedItem("a").value = "one";
    expect(isDirty(form, initial)).toBe(false);
  });

  it("sees form= attached controls living outside the form", () => {
    const form = build();
    const initial = snapshot(form);
    document.querySelector("textarea").value = "edited";
    expect(isDirty(form, initial)).toBe(true);
    document.querySelector("textarea").value = "two";
    document.querySelector("select").value = "y";
    expect(isDirty(form, initial)).toBe(true);
  });

  it("prompts on unload only while dirty, and never after submit", () => {
    const form = build();
    guard(form, window);
    expect(fireUnload().defaultPrevented).toBe(false);
    document.querySelector("textarea").value = "edited";
    expect(fireUnload().defaultPrevented).toBe(true);
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(fireUnload().defaultPrevented).toBe(false);
  });

  it("re-arms on pageshow after an aborted submit, and re-baselines on bfcache", () => {
    const form = build();
    guard(form, window);
    document.querySelector("textarea").value = "edited";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(fireUnload().defaultPrevented).toBe(false);
    window.dispatchEvent(new Event("pageshow"));
    expect(fireUnload().defaultPrevented).toBe(true);
    const restored = new Event("pageshow");
    restored.persisted = true;
    window.dispatchEvent(restored);
    expect(fireUnload().defaultPrevented).toBe(false);
  });
});
