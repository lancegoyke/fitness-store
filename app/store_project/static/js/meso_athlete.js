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
    // (Save progress); both upsert the same log.
    async save(markDone) {
      if (this.saving || !this.logUrl) return;
      this.saving = true;
      this.saved = false;
      this.error = false;
      try {
        const res = await fetch(this.logUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": this.csrf,
          },
          body: JSON.stringify(this.buildPayload(markDone)),
        });
        if (!res.ok) throw new Error("Request failed: " + res.status);
        const data = await res.json();
        this.status = data.log.status;
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
  }));
});
