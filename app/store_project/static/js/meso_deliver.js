/* Meso — deliver screen state. The "Deliver" button POSTs the real action
 * (stamp the target week + snapshot). `weekId` is the week the screen targets
 * (the current week, or the ?week= the coach picked); sending it lets a coach
 * deliver a built-ahead week without first making it current. Scheduling and
 * push/email notifications arrive with the athlete app.
 */
// Issue #451: after a fetch action that can auto-advance the guided tour
// server-side (delivering the coach's own self-link block), nudge the
// mounted meso_tour.js driver to re-read the authoritative step and
// re-render — the tour card would otherwise stay on the deliver step until
// the coach's next navigation. Best-effort + guarded: a real page has
// `document`, but the vitest import that pulls in the factory does not.
function notifyTourRefresh() {
  if (
    typeof document !== "undefined" &&
    typeof document.dispatchEvent === "function" &&
    typeof CustomEvent === "function"
  ) {
    document.dispatchEvent(new CustomEvent("meso:tour-refresh"));
  }
}

function createMesoDeliver(planId, csrf, weekId) {
  return {
    planId: planId,
    csrf: csrf,
    weekId: weekId,
    delivered: false,
    sending: false,
    error: false,
    async deliver() {
      this.sending = true;
      this.error = false;
      try {
        const res = await fetch(`/meso/api/plan/${this.planId}/deliver/`, {
          method: "POST",
          headers: {
            "X-CSRFToken": this.csrf,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(
            this.weekId != null ? { week_id: this.weekId } : {},
          ),
        });
        if (!res.ok) throw new Error("Request failed: " + res.status);
        this.delivered = true;
        // Success only (not the error/network paths below): the server may have
        // just advanced the coach's self-variant "deliver" tour step (#451).
        notifyTourRefresh();
      } catch (e) {
        this.error = true;
        console.error("Deliver failed", e);
      } finally {
        this.sending = false;
      }
    },
  };
}

// Register the Alpine component in the browser. Loaded as a classic <script>,
// so `document` exists here but no module system does.
if (
  typeof document !== "undefined" &&
  typeof document.addEventListener === "function"
) {
  document.addEventListener("alpine:init", () => {
    Alpine.data("mesoDeliver", createMesoDeliver);
  });
}

// Test hook: expose the factory to Node-based runners (vitest). Skipped in the
// browser, where `module` is undefined.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { createMesoDeliver };
}
