/* Meso — athlete session logger (athlete slice Phase 2).
 *
 * The athlete's delivered-session screen. init() hydrates the set rows from the
 * injected `meso-log-data` (pre-filled from the athlete's own existing log), the
 * athlete fills reps/load/rpe and checks sets off, and save() POSTs the whole
 * session to the log endpoint (api/me/session/<id>/log/). The write is idempotent
 * — re-saving updates the one log — so "Save progress" and "Log session" hit the
 * same endpoint, differing only in the status they stamp (pending vs done).
 */
// ---- %1RM ergonomics helpers (S2 Phase 2b) ----
// Pure maths shared by the logger and its tests. A %1RM target ("75%") is an
// intensity, not a weight; given the athlete's estimated 1RM these turn it into a
// bar load and back, so the athlete knows what to put on the bar.

// Parse a strictly-numeric cell to a Number, or null. Rejects the program grid's
// free-text loads/reps ("BW", "AMRAP", "8-10", "") that can't enter the maths.
function parseNum(text) {
  const s = String(text == null ? "" : text).trim();
  if (s === "" || !/^[0-9]*\.?[0-9]+$/.test(s)) return null;
  const n = parseFloat(s);
  return Number.isNaN(n) ? null : n;
}

// Format a computed number for display: a whole number stays integral, otherwise
// it's trimmed to 2 decimals (116.6666… → 116.67).
function fmtNum(n) {
  if (n == null || Number.isNaN(n)) return "";
  return Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
}

// Round to the nearest loadable step (2.5 for plates), matching the designer's
// round25 so a suggested load lands on a real plate.
function roundToStep(value, step) {
  return Math.round(value / step) * step;
}

// Estimated 1RM from a logged set via Epley: w × (1 + reps/30). A single rep IS a
// 1RM, so it returns the load unchanged (not the formula's slight overshoot).
// Null when either cell isn't a usable number (load > 0, reps ≥ 1).
function epleyOneRm(load, reps) {
  const w = parseNum(load);
  const r = parseNum(reps);
  if (w == null || r == null || w <= 0 || r < 1) return null;
  if (r === 1) return w;
  return w * (1 + r / 30);
}

// The bar load for a percent of an estimated 1RM, plate-rounded. Null without a
// usable 1RM and percent.
function loadForPercent(oneRm, percent) {
  const one = parseNum(oneRm);
  const pct = parseNum(percent);
  if (one == null || pct == null || one <= 0 || pct <= 0) return null;
  return roundToStep((one * pct) / 100, 2.5);
}

function createLogger() {
  return {
    logUrl: "",
    csrf: "",
    status: "pending",
    unit: "", // the plan's load unit (kg/lb), for the %1RM helper
    exercises: [],
    saving: false,
    saved: false,
    error: false,
    queued: false, // a save is stashed locally, waiting for the network

    init() {
      const el = document.getElementById("meso-log-data");
      if (!el) return; // nothing injected → inert (the page renders its fallback)
      let data;
      try {
        data = JSON.parse(el.textContent);
      } catch (e) {
        console.error("Could not parse log data", e);
        return;
      }
      this.logUrl = data.log_url;
      this.status = data.status;
      this.unit = data.unit || "";
      this.exercises = data.exercises || [];
      // Each exercise carries `one_rm` — the athlete's persisted, log-derived 1RM
      // (server) — and `e1rm`, an optional per-device typed override restored from
      // localStorage. The typed value wins when present; otherwise the derived
      // value seeds the suggested load with no manual entry needed.
      const e1rms = this.readE1rms();
      for (const ex of this.exercises) {
        ex.one_rm = ex.one_rm || "";
        ex.e1rm = e1rms[ex.id] || "";
      }
      const csrfEl = document.getElementById("meso-csrf");
      this.csrf = csrfEl ? csrfEl.dataset.token : "";
      // Flush anything logged while offline (S7), now and whenever wifi returns.
      this.flushQueue();
      window.addEventListener("online", () => this.flushQueue());
    },

    // ---- derived progress ----
    get totalSets() {
      return this.exercises.reduce((acc, e) => acc + e.set_rows.length, 0);
    },
    get doneSets() {
      return this.exercises.reduce(
        (acc, e) => acc + e.set_rows.filter((r) => r.done).length,
        0,
      );
    },

    toggle(row) {
      row.done = !row.done;
    },

    // A row is worth sending if it's checked or carries any entry.
    rowFilled(r) {
      return (
        r.done ||
        (r.reps || "") !== "" ||
        (r.load || "") !== "" ||
        (r.rpe || "") !== ""
      );
    },

    // Collect the filled rows into the endpoint's payload shape.
    buildPayload(markDone) {
      const sets = [];
      for (const e of this.exercises) {
        for (const r of e.set_rows) {
          if (!this.rowFilled(r)) continue;
          sets.push({
            prescription: e.id,
            set_number: r.set_number,
            reps: r.reps || "",
            load: r.load || "",
            rpe: r.rpe || "",
          });
        }
      }
      // "Log session" completes the session; "Save progress" keeps the current
      // status, so saving edits to an already-logged session never downgrades it
      // back to "To do".
      return { status: markDone ? "done" : this.status, sets };
    },

    // POST the session. `markDone` flips it to "done" (Log session) vs "pending"
    // (Save progress); both upsert the same log. When the network is unreachable
    // (flaky gym wifi — S7), the save is stashed locally and flushed on
    // reconnect instead of being lost; an HTTP error (the server answered) is a
    // real error the athlete should retry.
    async save(markDone) {
      if (this.saving || !this.logUrl) return;
      this.saving = true;
      this.saved = false;
      this.error = false;
      this.queued = false;
      const payload = this.buildPayload(markDone);
      // Reflect the intended status locally right away so the UI is responsive
      // whether the request lands now or after a sync.
      if (markDone) this.status = "done";
      let res;
      try {
        res = await fetch(this.logUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": this.csrf,
          },
          body: JSON.stringify(payload),
        });
      } catch (netErr) {
        // Network unreachable → queue it; the upsert endpoint is idempotent, so
        // replaying on reconnect is safe (latest save for a session wins).
        this.enqueue(payload);
        this.queued = true;
        this.saving = false;
        return;
      }
      try {
        // A redirect means the session expired and we were bounced to login —
        // the write never reached the endpoint (res.ok is true for the login
        // HTML). Don't lose it: queue for retry, where the next online flush
        // (after re-login) carries a fresh CSRF.
        if (res.redirected) {
          this.enqueue(payload);
          this.queued = true;
          return;
        }
        if (!res.ok) throw new Error("Request failed: " + res.status);
        const data = await res.json();
        this.status = data.log.status;
        this.syncFromLog(data.log);
        this.saved = true;
        setTimeout(() => {
          this.saved = false;
        }, 2400);
      } catch (err) {
        console.error("Log save failed", err);
        this.error = true;
      } finally {
        this.saving = false;
      }
    },

    // ---- offline queue (S7) ----
    // A tiny localStorage-backed outbox keyed by the session's log URL: one
    // pending save per session (the latest supersedes an earlier queued one), so
    // replaying after reconnect can't pile up duplicate writes.
    queueKey: "meso-log-queue",

    readQueue() {
      try {
        return JSON.parse(localStorage.getItem(this.queueKey) || "[]");
      } catch (e) {
        return [];
      }
    },

    writeQueue(items) {
      try {
        localStorage.setItem(this.queueKey, JSON.stringify(items));
      } catch (e) {
        console.error("Could not persist offline log queue", e);
      }
    },

    enqueue(payload) {
      const queue = this.readQueue().filter((item) => item.url !== this.logUrl);
      queue.push({ url: this.logUrl, body: payload });
      this.writeQueue(queue);
    },

    // Replay queued saves. Items that still fail (offline, or the server errored)
    // stay queued for the next attempt. Uses the live CSRF token, never a stale
    // stored one.
    async flushQueue() {
      const queue = this.readQueue();
      if (!queue.length) return;
      const remaining = [];
      let flushedMine = false;
      for (const item of queue) {
        let res;
        try {
          res = await fetch(item.url, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRFToken": this.csrf,
            },
            body: JSON.stringify(item.body),
          });
        } catch (netErr) {
          remaining.push(item); // still offline — keep it for next time
          continue;
        }
        // A redirect means we were bounced to login (expired session); res.ok is
        // true for the login HTML but the log was never saved — keep it queued
        // so a real re-login + flush delivers it instead of dropping the workout.
        if (res.redirected || !res.ok) {
          remaining.push(item);
          continue;
        }
        if (item.url === this.logUrl) {
          try {
            const data = await res.json();
            this.status = data.log.status;
            this.syncFromLog(data.log);
            flushedMine = true;
          } catch (e) {
            /* synced server-side regardless; UI reconciles on next load */
          }
        }
      }
      this.writeQueue(remaining);
      // If this session's queued save went through, clear the "will sync" hint.
      if (flushedMine && !remaining.some((i) => i.url === this.logUrl)) {
        this.queued = false;
        this.saved = true;
        setTimeout(() => {
          this.saved = false;
        }, 2400);
      }
    },

    // Reconcile the rows with what the server actually persisted so the check
    // circles and counter match the saved log immediately — without this, rows
    // that were sent because they carried data (but were never ticked) would
    // stay un-checked until a reload. The returned log is the source of truth.
    syncFromLog(log) {
      const saved = new Set(
        (log.sets || []).map((s) => `${s.prescription}:${s.set_number}`),
      );
      for (const e of this.exercises) {
        for (const r of e.set_rows) {
          r.done = saved.has(`${e.id}:${r.set_number}`);
        }
      }
    },

    // ---- %1RM ergonomics (S2 Phase 2b) ----
    // A %1RM-prescribed lift: the target Load is a percent of 1RM, not a weight.
    isPercentLift(ex) {
      return !!ex && ex.load_type === "pct";
    },

    // The 1RM to size a suggested load from: the athlete's typed per-device
    // estimate (localStorage) overrides the server's log-derived value when set;
    // absent a typed value, the derived 1RM is used so the suggestion appears with
    // no manual entry. Empty when neither is a usable number.
    effectiveOneRm(ex) {
      if (!ex) return "";
      return parseNum(ex.e1rm) != null ? ex.e1rm : ex.one_rm || "";
    },

    // True when the suggestion is sized off the server's derived 1RM with no typed
    // override in play — drives the "from your logs" hint.
    usingDerivedOneRm(ex) {
      return !!(ex && ex.one_rm && parseNum(ex.e1rm) == null);
    },

    // The suggested bar load for a %1RM lift given the athlete's estimated 1RM,
    // with the plan's unit ("90 kg"). Empty when it isn't a %1RM lift or no usable
    // 1RM is known (neither derived nor typed) yet.
    suggestedLoad(ex) {
      if (!this.isPercentLift(ex)) return "";
      const load = loadForPercent(this.effectiveOneRm(ex), ex.load);
      if (load == null) return "";
      return fmtNum(load) + (this.unit ? " " + this.unit : "");
    },

    // The 1RM a logged set implies (Epley), with the unit — shown on a %1RM lift so
    // the athlete can refine their estimate from what they actually lifted. Empty
    // until the set carries a numeric load + reps.
    setImpliedOneRm(row) {
      const one = epleyOneRm(row.load, row.reps);
      if (one == null) return "";
      return fmtNum(one) + (this.unit ? " " + this.unit : "");
    },

    // ---- estimated-1RM store (localStorage, keyed by exercise id) ----
    // The estimate is per-device convenience, not coach-owned program data, so it
    // lives client-side — same "reuse what exists, defer new tables" taste as the
    // offline log queue.
    e1rmKey: "meso-e1rm",

    readE1rms() {
      try {
        return JSON.parse(localStorage.getItem(this.e1rmKey) || "{}") || {};
      } catch (e) {
        return {};
      }
    },

    writeE1rms(map) {
      try {
        localStorage.setItem(this.e1rmKey, JSON.stringify(map));
      } catch (e) {
        console.error("Could not persist 1RM estimates", e);
      }
    },

    // Persist (or clear) one exercise's 1RM estimate as the athlete edits it.
    persistE1rm(ex) {
      const map = this.readE1rms();
      const value = (ex.e1rm || "").toString().trim();
      if (value === "") delete map[ex.id];
      else map[ex.id] = value;
      this.writeE1rms(map);
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
    Alpine.data("logger", () => createLogger());
  });
}

// Test hook: expose the factory to Node-based runners (vitest). Skipped in the
// browser, where `module` is undefined.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { createLogger, epleyOneRm, roundToStep, loadForPercent };
}
