/* Meso — athlete session logger (athlete slice Phase 2).
 *
 * The athlete's delivered-session screen. init() hydrates the set rows from the
 * injected `meso-log-data` (pre-filled from the athlete's own existing log), the
 * athlete fills reps/load/rpe and checks sets off, and save() POSTs the whole
 * session to the log endpoint (api/me/session/<id>/log/). The write is idempotent
 * — re-saving updates the one log — so "Save progress" and "Log session" hit the
 * same endpoint, differing only in the status they stamp (pending vs done).
 */
document.addEventListener("alpine:init", () => {
  Alpine.data("logger", () => ({
    logUrl: "",
    csrf: "",
    status: "pending",
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
      this.exercises = data.exercises || [];
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
        if (!res.ok) {
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
  }));
});
