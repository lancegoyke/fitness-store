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

// Issue #451: after a fetch action that can auto-advance the guided tour
// server-side (logging the coach's own session), nudge the mounted
// meso_tour.js driver to re-read the authoritative step and re-render — the
// tour card would otherwise stay on the results step until the coach's next
// navigation. Best-effort + guarded: a real page has `document`, but the
// vitest import that pulls in the factory does not.
function notifyTourRefresh() {
  if (
    typeof document !== "undefined" &&
    typeof document.dispatchEvent === "function" &&
    typeof CustomEvent === "function"
  ) {
    document.dispatchEvent(new CustomEvent("meso:tour-refresh"));
  }
}

// The athlete's freeform sub-line stack per exercise is capped at this many
// lines (matches the server's MAX_CELL_LINE) so `addLine` can't fabricate an
// unbounded stack.
const MAX_CELL_LINE = 20;

// The offline outbox (`meso-log-queue`) holds two kinds of write. A session
// log is `{url, body}`, the shape it has always had, so a queue written before
// #527 still replays. A typed line is `{kind: "cell", url, body: {exercise_id,
// line, text}}`: one entry per cell, since the cell url names the session and
// the exercise id names the row and week.
function isCellEntry(item) {
  return !!item && item.kind === "cell" && !!item.body;
}

function isSameCell(item, url, exerciseId, line) {
  return (
    isCellEntry(item) &&
    item.url === url &&
    item.body.exercise_id === exerciseId &&
    item.body.line === line
  );
}

// A line write the server couldn't take right now, as opposed to one it read
// and refused: it failed (5xx), timed out (408) or asked us to slow down (429).
// Worth sending again as it is.
function isRetryableStatus(status) {
  return status >= 500 || status === 408 || status === 429;
}

// How long a logging write may take before it counts as offline (#527). fetch
// has no timeout of its own, and gym wifi that connects but never answers would
// hold a write open forever — and "Log session" with it, since it waits for the
// lines — with nothing queued. The writes are idempotent, so one that landed
// after all and is sent again does no harm. The timer isn't cleared: firing
// after the exchange finished does nothing, and it also bounds the body read.
const WRITE_TIMEOUT_MS = 15000;

function postJson(url, body, csrf) {
  const options = {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrf,
    },
    body: JSON.stringify(body),
  };
  if (typeof AbortController === "function") {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), WRITE_TIMEOUT_MS);
    options.signal = controller.signal;
  }
  return fetch(url, options);
}

function createLogger() {
  return {
    logUrl: "",
    oneRmUrl: "", // where a manually-entered 1RM is persisted server-side (Phase 2)
    cellUrl: "", // where the athlete's freeform sub-line cells are upserted (Phase 4a)
    csrf: "",
    owner: "", // the signed-in athlete; only their queued writes are flushed here
    status: "pending",
    unit: "", // the plan's load unit (kg/lb), for the %1RM helper
    exercises: [],
    saving: false,
    saved: false,
    error: false,
    queued: false, // a save is stashed locally, waiting for the network
    lineError: false, // the log landed, but a line the server refused didn't
    newRecords: [], // PRs the last save beat (Phase 4c) — the celebration toast
    _oneRmTimers: {}, // per-exercise debounce handles for the manual-1RM POST
    _cellSaves: {}, // per-cell promise chain, so blurs reach the server in order
    _blurSaves: 0, // line saves started by a blur, for `settleLines`
    _flushing: null, // the flush pass in progress, if any
    _flushAgain: false, // a flush was asked for mid-pass; run one more

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
      this.oneRmUrl = data.one_rm_url || "";
      this.cellUrl = data.cell_url || "";
      this.owner = data.owner || "";
      this.status = data.status;
      this.unit = data.unit || "";
      this.exercises = data.exercises || [];
      // Default the freeform tracking stack (Phase 4a) so the template's
      // `x-for` over `ex.sub_lines` is safe even for an exercise with none.
      //
      // ...and open with ONE EMPTY LINE PER PRESCRIBED SET, so the stack the
      // athlete types into lines up with the sets they were asked for. Blank
      // cells aren't persisted (the presenter drops them), so an exercise with
      // nothing typed yet arrives EMPTY — which rendered as a bare "+ add a
      // line" button under three labelled set inputs, and nobody would choose
      // to put their data there. `set_rows` is already sized to the
      // prescription server-side (its own default and caps applied), so
      // matching it keeps one source of truth for "how many sets is this".
      //
      // Gaps are filled by NUMBER, not appended: a cleared line leaves a hole
      // (the server drops the blank but keeps later ones), and `line` is also
      // the parsed set's `set_number`, so a stack must rebuild identically on
      // every reload. Filling 1..n by number does that; appending would walk
      // the numbers up on each visit. Lines the athlete has beyond the
      // prescription are always kept.
      for (const ex of this.exercises) {
        if (!Array.isArray(ex.sub_lines)) ex.sub_lines = [];
        const rows = Array.isArray(ex.set_rows) ? ex.set_rows.length : 0;
        const want = Math.min(Math.max(rows, 1), MAX_CELL_LINE);
        const present = new Set(ex.sub_lines.map((l) => l.line));
        for (let n = 1; n <= want; n += 1) {
          if (!present.has(n)) ex.sub_lines.push({ line: n, text: "" });
        }
        ex.sub_lines.sort((a, b) => (a.line || 0) - (b.line || 0));
        // What the server holds for each line, so a blur that changes nothing
        // posts nothing (#527). A padded line holds "" — the server has no
        // text there, or only blank text, which it doesn't render.
        for (const l of ex.sub_lines) {
          l.savedText = l.text || "";
          l.queued = false;
          l.saveError = false;
        }
      }
      // Each exercise carries the athlete's persisted 1RM (`one_rm`) and its
      // `one_rm_source`. A `manual` value is the athlete's own number — it seeds
      // the editable `e1rm` input. A `logged` value is auto-derived from their
      // logs — it stays in `one_rm` as the placeholder + suggested-load default,
      // with the input blank so a manual override layers cleanly on top.
      for (const ex of this.exercises) {
        const value = ex.one_rm || "";
        if (ex.one_rm_source === "manual") {
          ex.e1rm = value;
          ex.one_rm = "";
        } else {
          ex.e1rm = "";
          ex.one_rm = value;
        }
      }
      const csrfEl = document.getElementById("meso-csrf");
      this.csrf = csrfEl ? csrfEl.dataset.token : "";
      // One-time: promote any 1RM override typed before Phase 2 (per-device
      // `meso-e1rm` localStorage) to the server, so the upgrade doesn't silently
      // drop it.
      this.migrateLocalOverrides();
      // Show lines typed offline on an earlier visit as the athlete left them,
      // then flush everything logged while offline (S7, #527), now and
      // whenever wifi returns.
      this.restoreQueuedLines();
      this.flushQueue();
      window.addEventListener("online", () => this.flushQueue());
    },

    // Promote a pre-Phase-2 override (the retired `meso-e1rm` localStorage store,
    // keyed by exercise id) into the editable input and persist it server-side,
    // for any lift that doesn't already have a server-side manual value. Best-
    // effort, then the local store is dropped so a stale value can't later
    // resurrect over a cleared one. A no-op (and harmless) once the store is gone.
    migrateLocalOverrides() {
      let legacy;
      try {
        legacy = JSON.parse(localStorage.getItem("meso-e1rm") || "{}") || {};
      } catch (e) {
        legacy = {};
      }
      if (!legacy || !Object.keys(legacy).length) return;
      for (const ex of this.exercises) {
        const v = (legacy[ex.id] || "").toString().trim();
        if (v && parseNum(v) != null && ex.one_rm_source !== "manual") {
          ex.e1rm = v;
          ex.one_rm = "";
          this._postOneRm(ex); // fire-and-forget; the value also stays in-session
        }
      }
      try {
        localStorage.removeItem("meso-e1rm");
      } catch (e) {
        /* the store is best-effort; the values are already seeded in-session */
      }
    },

    // Fold lines still queued from an earlier visit into this page (#527). The
    // server renders the text it last saved, which is older than what the
    // athlete typed offline: showing that would tell them their set is gone,
    // and a blur of the line would post the old text back over the queued one.
    restoreQueuedLines() {
      if (!this.cellUrl) return;
      for (const item of this.readQueue()) {
        if (!isCellEntry(item) || item.url !== this.cellUrl) continue;
        if (!this.isMine(item)) continue;
        const ex = this.exercises.find((e) => e.id === item.body.exercise_id);
        // Gone from the session: the flush sends it and the server says so.
        if (!ex) continue;
        const line = item.body.line;
        if (!ex.sub_lines.some((l) => l.line === line)) {
          ex.sub_lines.push({ line, text: "", savedText: "" });
          ex.sub_lines.sort((a, b) => (a.line || 0) - (b.line || 0));
        }
        // Read back through the array, so on the live page this is Alpine's
        // reactive copy rather than the plain object just pushed.
        const entry = ex.sub_lines.find((l) => l.line === line);
        entry.text = item.body.text;
        entry.queued = true;
        entry.saveError = false;
      }
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

    // One PR line for the celebration toast (Phase 4c). The server preformatted
    // the numbers (value/delta), so this only assembles them — never re-rounds.
    prLabel(pr) {
      const base = pr.name + " — " + pr.value + " " + pr.unit;
      return pr.is_first ? base + " (first best)" : base + " (+" + pr.delta + ")";
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
      this.lineError = false;
      this.newRecords = []; // clear any prior toast; this save recomputes it
      const payload = this.buildPayload(markDone);
      // Reflect the intended status locally right away so the UI is responsive
      // whether the request lands now or after a sync.
      if (markDone) this.status = "done";
      // Lines first (#527). Pressing the button blurs the line being typed, so
      // its save is already on its way: let it land, then send any line still
      // queued from earlier. The log then reaches the server after the sets its
      // lines carry, one request at a time, and what this save reports below
      // covers the lines too.
      await this.settleLines();
      let res;
      try {
        res = await postJson(this.logUrl, payload, this.csrf);
      } catch (netErr) {
        // Network unreachable → queue it; the upsert endpoint is idempotent, so
        // replaying on reconnect is safe (latest save for a session wins).
        this.keepForLater(payload);
        this.saving = false;
        return;
      }
      try {
        // A redirect means the session expired and we were bounced to login —
        // the write never reached the endpoint (res.ok is true for the login
        // HTML). Don't lose it: queue for retry, where the next online flush
        // (after re-login) carries a fresh CSRF.
        if (res.redirected) {
          this.keepForLater(payload);
          return;
        }
        if (!res.ok) throw new Error("Request failed: " + res.status);
        const data = await res.json();
        this.status = data.log.status;
        this.syncFromLog(data.log);
        // Any lift this save beat. As of 5a the records read is LIVE — it counts
        // pending sets too — so a "Save progress" no longer comes back empty and
        // can legitimately surface a toast before the session is ever done.
        this.newRecords = data.new_records || [];
        this.reportSaved();
        // Key the tour nudge off the log status the *server* persisted, not the
        // button pressed (#451): the self-variant "results" step advances on a
        // `done` log (`advance_self_step_if_complete("results")`), and a "Save
        // progress" on an already-completed session still writes `done` — so
        // `markDone` alone would leave the card stale after re-saving a logged
        // session. A pending save returns `pending` → no spurious re-render /
        // screen-reader re-announcement (the offline `flushQueue` path stays
        // silent regardless — it never calls this).
        if (data.log.status === "done") notifyTourRefresh();
      } catch (err) {
        console.error("Log save failed", err);
        this.error = true;
      } finally {
        this.saving = false;
      }
    },

    // Say what's true (#527). "Saved ✓" only once every write this page made
    // has landed: a line still queued keeps the "will sync" message up, and a
    // line the server refused gets its own warning instead of a tick.
    reportSaved() {
      this.saved = false;
      this.queued = false;
      this.lineError = false;
      if (this.hasQueuedWrites()) {
        this.queued = true;
        return;
      }
      if (this.hasRefusedLines()) {
        this.lineError = true;
        return;
      }
      this.saved = true;
      setTimeout(() => {
        this.saved = false;
      }, 2400);
    },

    // True while this page has a write waiting in the outbox: a line, or the
    // session's own log.
    hasQueuedWrites() {
      const lineQueued = this.exercises.some((e) =>
        (e.sub_lines || []).some((l) => l.queued),
      );
      return (
        lineQueued ||
        this.readQueue().some(
          (i) => !isCellEntry(i) && i.url === this.logUrl && this.isMine(i),
        )
      );
    },

    hasRefusedLines() {
      return this.exercises.some((e) =>
        (e.sub_lines || []).some((l) => l.saveError),
      );
    },

    // Let every line save in flight land, then flush the outbox, so whatever
    // follows is sent after the lines (#527). The lines stay editable while
    // this waits, so a blur can start a save meanwhile: go round again until
    // one passes without. Only blurs count — the flush's own replays start
    // saves too, and counting those would loop for as long as it's offline.
    async settleLines() {
      let blurs;
      do {
        blurs = this._blurSaves;
        await Promise.all(
          Object.values(this._cellSaves).map((p) => p.catch(() => {})),
        );
        await this.flushQueue();
      } while (this._blurSaves !== blurs);
    },

    // ---- offline queue (S7, #527) ----
    // A tiny localStorage-backed outbox. One pending save per session log, and
    // one per typed line (the latest supersedes an earlier queued one), so
    // replaying after reconnect can't pile up duplicate writes.
    queueKey: "meso-log-queue",

    // An entry this page may send: queued by the athlete signed in now, or
    // queued before entries carried an owner. localStorage outlasts a logout,
    // so another athlete's writes can be sitting here; sent under this login
    // they'd be refused, and a refused line is dropped. They wait for their
    // owner instead.
    isMine(item) {
      return !item.owner || !this.owner || item.owner === this.owner;
    },

    // What every entry this page queues carries: its athlete, so only they
    // flush it (`isMine`), and an id of its own. Entries are told apart by
    // that id, not their text — another tab can queue the very text an older
    // entry held, and it's still the newer write.
    stamp(item) {
      const id =
        Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
      const stamped = { ...item, id };
      if (this.owner) stamped.owner = this.owner;
      return stamped;
    },

    readQueue() {
      try {
        return JSON.parse(localStorage.getItem(this.queueKey) || "[]");
      } catch (e) {
        return [];
      }
    },

    // True when the queue was written. Storage can be full or blocked, and
    // then nothing is kept: a caller must not tell the athlete it was.
    writeQueue(items) {
      try {
        localStorage.setItem(this.queueKey, JSON.stringify(items));
        return true;
      } catch (e) {
        console.error("Could not persist offline log queue", e);
        return false;
      }
    },

    // Queue this session's log and say so — or, when storage refused it, say
    // it didn't save.
    keepForLater(payload) {
      if (this.enqueue(payload)) this.queued = true;
      else this.error = true;
    },

    enqueue(payload) {
      const queue = this.readQueue().filter((item) => item.url !== this.logUrl);
      queue.push(this.stamp({ url: this.logUrl, body: payload }));
      return this.writeQueue(queue);
    },

    // Queue one line's write, replacing any earlier one for the same cell:
    // retyping a line offline overwrites it rather than stacking a second set.
    // Returns the entry as stored, or null when storage refused it.
    enqueueCell(body) {
      const queue = this.readQueue().filter(
        (item) => !isSameCell(item, this.cellUrl, body.exercise_id, body.line),
      );
      const item = this.stamp({ kind: "cell", url: this.cellUrl, body });
      queue.push(item);
      return this.writeQueue(queue) ? item : null;
    },

    queuedCell(exerciseId, line) {
      return this.readQueue().find((item) =>
        isSameCell(item, this.cellUrl, exerciseId, line),
      );
    },

    // Remove one replayed entry, but only as it was sent: a newer write queued
    // for the same log or cell while this one was in flight must survive.
    dropEntry(sent) {
      const key = JSON.stringify(sent);
      const queue = this.readQueue();
      const index = queue.findIndex((item) => JSON.stringify(item) === key);
      if (index === -1) return;
      queue.splice(index, 1);
      this.writeQueue(queue);
    },

    // Replay the outbox. `init()`, `online` and `save()` can all ask at once,
    // but only one pass runs at a time: two would send the same entries twice,
    // and concurrently. A request that arrives mid-pass gets one more pass
    // after it, so a write queued in between isn't left behind.
    flushQueue() {
      if (this._flushing) {
        this._flushAgain = true;
        return this._flushing;
      }
      this._flushing = (async () => {
        try {
          do {
            this._flushAgain = false;
            await this.flushPass();
          } while (this._flushAgain);
        } finally {
          this._flushing = null;
        }
      })();
      return this._flushing;
    },

    // One pass over the outbox, ONE REQUEST AT A TIME, lines before logs
    // (#527). A session log that lands before its lines' sets exist would come
    // back without them, and two requests at once is exactly the collision the
    // e2e suite's shared SQLite connection can't take. The first network
    // failure or login redirect ends the pass: everything after it would fail
    // the same way, and stopping keeps a log from overtaking its lines. Items
    // that fail stay queued. Uses the live CSRF token, never a stale stored
    // one.
    async flushPass() {
      const queue = this.readQueue().filter((item) => this.isMine(item));
      if (!queue.length) return;
      for (const item of queue.filter(isCellEntry)) {
        if ((await this.flushCell(item)) === "offline") return;
      }
      let flushedMine = false;
      for (const item of queue.filter((i) => !isCellEntry(i))) {
        const outcome = await this.flushLog(item);
        if (outcome === "offline") break;
        if (outcome === "mine") flushedMine = true;
      }
      // A pass that lands this session's log, or the last line a "will sync"
      // message was waiting on, updates the message. Not mid-`save()`, though:
      // save reports once its own log is in.
      if (!this.saving && (flushedMine || this.queued)) this.reportSaved();
    },

    // A queued line on this page goes through that line's own save chain, so
    // it can't race a blur of the same line. One from another page has no line
    // to update; it's sent as it was queued.
    async flushCell(item) {
      const ex =
        item.url === this.cellUrl
          ? this.exercises.find((e) => e.id === item.body.exercise_id)
          : null;
      if (ex) return this.saveCell(ex, item.body.line, { fromQueue: true });
      let res;
      try {
        res = await postJson(item.url, item.body, this.csrf);
      } catch (netErr) {
        return "offline";
      }
      if (res.redirected || res.status === 403) return "offline";
      if (isRetryableStatus(res.status)) return "kept";
      // Refused for good (a 4xx won't change on retry), but a line from
      // another session is dropped only on its own page, where the athlete
      // sees it didn't save; here nothing would say so. On its own page with
      // its row gone, there's no line left to show it on.
      if (!res.ok && item.url !== this.cellUrl) return "kept";
      this.dropEntry(item);
      return res.ok ? "saved" : "rejected";
    },

    async flushLog(item) {
      let res;
      try {
        res = await postJson(item.url, item.body, this.csrf);
      } catch (netErr) {
        return "offline"; // still offline — keep it for next time
      }
      // A redirect means we were bounced to login (expired session); res.ok is
      // true for the login HTML but the log was never saved — keep it queued
      // so a real re-login + flush delivers it instead of dropping the workout.
      if (res.redirected) return "offline";
      if (!res.ok) return "kept";
      this.dropEntry(item);
      if (item.url !== this.logUrl) return "saved";
      try {
        const data = await res.json();
        this.status = data.log.status;
        this.syncFromLog(data.log);
        this.newRecords = data.new_records || []; // a PR beaten offline still lands
      } catch (e) {
        /* synced server-side regardless; UI reconciles on next load */
      }
      return "mine";
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
    // Text-first cells (Phase 2a): the prescription is one freeform string
    // ("4 x 6, RPE 7, 72%"), so the percent target is recovered from the text
    // — the first "NN%" token — instead of the retired load/load_type fields.
    percentTarget(ex) {
      if (!ex || !ex.text) return null;
      const m = /(\d+(?:\.\d+)?)\s*%/.exec(ex.text);
      return m ? m[1] : null;
    },

    // A %1RM-prescribed lift: its text carries a percent-of-1RM target.
    isPercentLift(ex) {
      return this.percentTarget(ex) != null;
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
      const load = loadForPercent(this.effectiveOneRm(ex), this.percentTarget(ex));
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

    // ---- manual 1RM persistence (server-side, Phase 2) ----
    // The athlete's typed 1RM was per-device localStorage (Phase 2b); it now
    // persists server-side as a `source=manual` row so it syncs across devices
    // and the coach can see it. Debounced so a quick edit doesn't POST every
    // keystroke.
    saveOneRm(ex) {
      if (!this.oneRmUrl || !ex) return;
      if (this._oneRmTimers[ex.id]) clearTimeout(this._oneRmTimers[ex.id]);
      this._oneRmTimers[ex.id] = setTimeout(() => this._postOneRm(ex), 600);
    },

    // POST one exercise's manual 1RM. A blank value clears it (reverting to the
    // server's log-derived estimate); a non-blank non-numeric value isn't worth a
    // round-trip (the server would 400 it). Best-effort: an unreachable network or
    // an error leaves the typed value in this session and retries on the next edit.
    async _postOneRm(ex) {
      if (!this.oneRmUrl || !ex) return;
      const value = (ex.e1rm || "").toString().trim();
      if (value !== "" && parseNum(value) == null) return;
      let res;
      try {
        res = await fetch(this.oneRmUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": this.csrf,
          },
          body: JSON.stringify({ prescription: ex.id, value }),
        });
      } catch (netErr) {
        return; // offline — keep the in-session value; next edit re-attempts
      }
      if (res.redirected || !res.ok) return;
      let data;
      try {
        data = await res.json();
      } catch (e) {
        return; // stored server-side regardless; the UI reconciles on next load
      }
      // Drop a stale response: if the field changed since we sent this value, a
      // newer edit (already sent, or still debouncing) owns it — reconciling now
      // would wipe the in-progress value (e.g. a lagging clear over a fresh type).
      if ((ex.e1rm || "").toString().trim() !== value) return;
      // Reconcile with what the server stored: a manual value stays in the input
      // as the server's *normalized* form (140.999 → "141"), so the suggested
      // load matches what's persisted; a cleared one reverts to the log-derived
      // estimate.
      if (data.source === "manual") {
        ex.e1rm = data.one_rm || ex.e1rm;
        ex.one_rm = "";
      } else {
        ex.e1rm = "";
        ex.one_rm = data.one_rm || "";
      }
    },

    // ---- freeform sub-line tracking (Phase 4a) ----
    // The athlete keeps an editable stack beneath each exercise — a free input
    // per line, saved on blur. `addLine` appends an empty sub-line (capped at
    // MAX_CELL_LINE); `saveCell` upserts one (exercise_id, line, text) cell.

    // Append an empty sub-line to the exercise's stack, up to MAX_CELL_LINE.
    addLine(ex) {
      if (!ex) return;
      if (!Array.isArray(ex.sub_lines)) ex.sub_lines = [];
      // Number off the MAX existing line, not the length: a sparse stack (the
      // server dropped a cleared line but kept a later one) would otherwise
      // fabricate a duplicate line number, breaking Alpine keys and `saveCell`
      // targeting. The cap is against that max too.
      const maxLine = ex.sub_lines.reduce((m, l) => Math.max(m, l.line || 0), 0);
      if (maxLine >= MAX_CELL_LINE) return;
      // Nothing is saved on a new line, so blurring it empty posts nothing.
      ex.sub_lines.push({ line: maxLine + 1, text: "", savedText: "" });
    },

    // Serialize saves PER CELL. Two blurs for one sub-line can otherwise be in
    // flight together and land at the server out of order — the older request
    // last — so `athlete_cell_write` saves the stale text and re-parses its
    // LoggedSet from it. Ignoring the stale *response* isn't enough: the UI
    // would show the correction while the database and the records kept the
    // stale parse, and a reload would surface it. Chaining means the newer text
    // is always written second, and since the body is read at send time an
    // intermediate blur simply coalesces into the latest value. A queued write
    // being replayed (`fromQueue`) joins the same chain. Resolves to how the
    // save went (see `_postCell`).
    saveCell(ex, line, { fromQueue = false } = {}) {
      if (!this.cellUrl || !ex) return Promise.resolve("skipped");
      if (!fromQueue) this._blurSaves += 1;
      const key = ex.id + ":" + line;
      const previous = this._cellSaves[key] || Promise.resolve();
      const run = previous
        .catch(() => {}) // a failed save must not stall the cell's queue
        .then(() => this._postCell(ex, line, fromQueue))
        .then((outcome) => {
          // A line landing can change what the footer last said: once no
          // line is queued or refused, "will sync" or "a line couldn't save"
          // gives way — no second "Log session" needed — and a line that
          // failed after "Saved ✓" went up takes it down. `save()` reports
          // for itself, after its own log.
          if (!this.saving && (this.queued || this.lineError || this.saved)) {
            this.reportSaved();
          }
          return outcome;
        });
      this._cellSaves[key] = run;
      return run;
    },

    // POST one exercise's sub-line cell. Blank text clears the cell in place
    // (the server never deletes a sub-line). Resolves to the outcome the flush
    // acts on:
    //
    //   "saved"    the server has this text.
    //   "offline"  the network is down, or the request was bounced to login or
    //              failed its CSRF check (a stale token) — the write never
    //              reached the endpoint. It's queued (#527) and the line says
    //              it will sync.
    //   "kept"     a 5xx (or 408/429): the server failed, not the write.
    //              Queued the same way.
    //   "rejected" any other 4xx: the server read the write and refused it, so
    //              the same text can't succeed later. Not queued; the line says
    //              "couldn't save" and the athlete's next edit re-attempts.
    //   "skipped"  nothing to send.
    async _postCell(ex, line, fromQueue = false) {
      const entry = (ex.sub_lines || []).find((l) => l.line === line);
      let text;
      // The outbox entry this write stands for.
      let sent = null;
      if (fromQueue) {
        // Send what the queue holds for this cell NOW. A blur that ran first
        // may already have saved newer text and dropped the entry; replaying
        // the older text would overwrite it.
        sent = this.queuedCell(ex.id, line);
        if (!sent) return "skipped";
        text = sent.body.text;
      } else {
        text = entry ? entry.text || "" : "";
        // Don't POST a line whose text the server already has (#527): tabbing
        // through a blank line or leaving a coach's line as it was changes
        // nothing, and offline it painted a spurious "couldn't save". A queued
        // line always posts — its text may match, but the queue must drain.
        // So does a warned one: set-shaped text saved while its row was
        // skipped has no set, and re-sending the same text once the coach
        // un-skips the row is how it gets one.
        if (
          entry &&
          entry.savedText !== undefined &&
          text === entry.savedText &&
          !entry.queued &&
          !entry.warn
        ) {
          entry.saveError = false;
          return "skipped";
        }
      }
      const body = { exercise_id: ex.id, line, text };
      // Write ahead: the line is in the outbox BEFORE the request goes out,
      // so closing the page mid-request — a POST stalled on gym wifi — can't
      // lose it. It replaces any older entry for the cell (latest text wins);
      // success or a refusal takes it back out. Only this entry, by its id:
      // another tab on the session may queue newer text for the line
      // meanwhile, and that one stays.
      if (!fromQueue) {
        const older = this.queuedCell(ex.id, line);
        sent = this.enqueueCell(body);
        // Storage full or blocked: this text can't be queued, and an older
        // entry left in its place would replay over it later. Latest wins,
        // so it goes (removing shrinks the queue, which a full store allows).
        if (!sent && older) this.dropEntry(older);
      }
      if (entry) entry.saveError = false;
      let res;
      try {
        res = await postJson(this.cellUrl, body, this.csrf);
      } catch (netErr) {
        this._holdCell(entry, ex.id, line, !!sent);
        return "offline";
      }
      if (res.redirected || res.status === 403) {
        this._holdCell(entry, ex.id, line, !!sent);
        return "offline";
      }
      if (isRetryableStatus(res.status)) {
        this._holdCell(entry, ex.id, line, !!sent);
        return "kept";
      }
      if (!res.ok) {
        if (sent) this.dropEntry(sent);
        if (entry) {
          entry.queued = false;
          entry.saveError = true;
        }
        return "rejected";
      }
      if (sent) this.dropEntry(sent);
      if (entry) entry.queued = false;
      let data;
      try {
        data = await res.json();
      } catch (e) {
        // Saved server-side regardless, but its warn/PR state is unknown, so
        // the line stays dirty: the next blur sends it again (harmlessly)
        // and reconciles.
        return "saved";
      }
      if (entry) entry.savedText = text;
      // Drop a stale response. Two saves for the same sub-line can be in
      // flight at once, and the older one can land last — so fixing `225 x`
      // to `225 x 5` could re-apply the first reply's warn and leave the cell
      // tinted for text that no longer exists. Only trust a reply whose text
      // is still what's in the input.
      // Derive-on-read warn (5a §8): re-classified server-side from the
      // just-committed text, so fixing a fat-fingered attempt (or typing one)
      // updates the cell's color right away, without a page reload.
      if (!entry || (entry.text || "") !== text) return "saved";
      entry.warn = !!(data.cell && data.cell.warn);
      // Optimistic PR (5a §7), marked ON THE LINE THAT EARNED IT rather than in
      // `newRecords`. That card renders at the top of the page, which is right
      // for `save()` — "Log session" is a whole-session act — but wrong here: a
      // blur happens wherever the athlete is typing, and UAT found the
      // celebration firing off-screen every time. The point of the optimistic
      // path is feedback in the moment, so it belongs beside the cell, in the
      // same slot as this line's other status labels.
      //
      // Cleared when the line no longer wins anything, so correcting a set down
      // takes its badge with it — derive-on-read, exactly like `warn`.
      const earned =
        Array.isArray(data.new_records) && data.new_records.length
          ? data.new_records[0]
          : null;
      entry.pr = earned ? `${earned.value} ${earned.unit}` : "";
      return "saved";
    },

    // A line's write didn't land; say what the outbox holds for it. Written
    // ahead, it's there for the next flush — unless storage refused it, and
    // then nothing will sync, so the line says it couldn't save. (If another
    // tab flushed it meanwhile, it's neither: the line stays dirty and the
    // next blur sends it again.)
    _holdCell(entry, exerciseId, line, persisted) {
      if (!entry) return;
      const held = !!this.queuedCell(exerciseId, line);
      entry.queued = held;
      entry.saveError = !held && !persisted;
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
