/* Meso — athlete session logger.
 *
 * The athlete's delivered-session screen. The athlete logs by TYPING A LINE per
 * set under "what you did" ("225 x 5, RPE 8"); each line saves on blur through
 * the cell endpoint (saveCell/_postCell), which the server parses into a set.
 * finish() POSTs `{status: "done"}` to the log endpoint (api/me/session/<id>/log/)
 * to complete the session. The server counts the sets the lines became and
 * sends the count back as `progress`, which `progressLabel` shows.
 *
 * Two things sit beside the athlete's lines. The coach's own cues for an exercise
 * (`coach_lines`) are read-only text after the sets: they occupy line numbers, so
 * the athlete's stack only ever uses the FREE ones (the cell endpoint refuses a
 * changed write onto a coach line). And one session note ("Notes for your coach")
 * saves through the log endpoint as `{notes}` — see `noteInput`/`saveNotes`.
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
// Null when either cell isn't a usable number (load > 0, reps ≥ 1). The page no
// longer calls it (the per-set implied-1RM hint went with the Set rows); it
// stays exported, with its tests.
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

// A line write that never reached the endpoint as its own account: bounced to
// login (an expired session), a failed CSRF check (a stale token, e.g. on a
// page the service worker cached), or a 409 because another account is signed
// in now. The write itself is fine; it waits for its owner.
function isWrongAccount(res) {
  return res.redirected || res.status === 403 || res.status === 409;
}

// A line write the server couldn't take right now, as opposed to one it read
// and refused: it failed (5xx), timed out (408) or asked us to slow down (429).
// Worth sending again as it is.
function isRetryableStatus(status) {
  return status >= 500 || status === 408 || status === 429;
}

// How long a logging write may take before it counts as offline (#527). fetch
// has no timeout of its own, and gym wifi that connects but never answers would
// hold a write open forever — and "Finish session" with it, since it waits for
// the lines — with nothing queued. The writes are idempotent, so one that landed
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
    // What `status` was before the save whose entry is still in the outbox
    // flipped it optimistically — so a flush that the server later REFUSES
    // can put the badge back (see `flushLog`). Empty once nothing of this
    // page's is queued any more.
    statusBeforeQueued: "",
    queued: false, // a save is stashed locally, waiting for the network
    lineError: false, // the log landed, but a line the server refused didn't
    // Sets logged vs prescribed across the session, as the server counts them
    // (`applyProgress`): shown by `progressLabel`.
    progress: { logged: 0, prescribed: 0 },
    _progressAsOf: 0, // `as_of` of the last progress applied (see `applyProgress`)
    _oneRmTimers: {}, // per-exercise debounce handles for the manual-1RM POST
    _cellSaves: {}, // per-cell promise chain, so blurs reach the server in order
    _blurSaves: 0, // line saves started by a blur, for `settleLines`
    _lineSavesRunning: {}, // per cell: saves chained and not yet finished
    _ownEntries: {}, // ids of the outbox entries this page wrote or restored
    _flushing: null, // the flush pass in progress, if any
    _flushAgain: false, // a flush was asked for mid-pass; run one more
    notes: "", // the athlete's note to their coach for the whole session
    notesMax: 2000,
    _notesSavedText: "", // the note as the server last confirmed it
    noteStatus: "", // "", "saved", "queued" or "error" — the note's own footer
    _noteTimer: null, // debounce handle for the note POST
    _notesSave: Promise.resolve(), // promise chain: note POSTs reach the server in order
    _notesRunning: 0, // note saves chained and not yet finished
    _notesDirty: false, // this tab's textarea holds text typed here, not yet confirmed

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
      this.applyProgress(data.progress);
      this.notes = data.notes || "";
      this.notesMax = data.notes_max || 2000;
      this._notesSavedText = this.notes;
      // Default the freeform tracking stack (Phase 4a) so the template's
      // `x-for` over `ex.sub_lines` is safe even for an exercise with none.
      //
      // ...and open with ONE EMPTY LINE PER PRESCRIBED SET, so the stack the
      // athlete types into lines up with the sets they were asked for. Blank
      // cells aren't persisted (the presenter drops them), so an exercise with
      // nothing typed yet arrives EMPTY — which would render as a bare "+ add a
      // line" button, and nobody would choose to put their data there. The
      // server sends the prescribed count as `pad_lines` (its own default and
      // caps applied), so the server stays the one source of truth for "how
      // many sets is this"; the client only clamps it to 1..MAX_CELL_LINE.
      //
      // Gaps are filled by NUMBER, not appended: a cleared line leaves a hole
      // (the server drops the blank but keeps later ones), and `line` is also
      // the parsed set's `set_number`, so a stack must rebuild identically on
      // every reload. Filling 1..n by number does that; appending would walk
      // the numbers up on each visit. Lines the athlete has beyond the
      // prescription are always kept.
      //
      // The coach's cues hold line numbers of their own (`coach_lines`; an
      // older server mid-deploy sends none), and the athlete's stack must never
      // land on one — the cell endpoint refuses a changed write there. So the
      // padding takes the first `want` FREE numbers instead of 1..want, still
      // by number and still the same on every reload: coach cues on line 1 and
      // a prescription of 3 pad lines 2, 3 and 4.
      for (const ex of this.exercises) {
        if (!Array.isArray(ex.sub_lines)) ex.sub_lines = [];
        if (!Array.isArray(ex.coach_lines)) ex.coach_lines = [];
        const want = Math.min(
          Math.max(Number.isInteger(ex.pad_lines) ? ex.pad_lines : 1, 1),
          MAX_CELL_LINE,
        );
        const present = new Set(ex.sub_lines.map((l) => l.line));
        const taken = new Set(ex.coach_lines.map((c) => c.line));
        let padded = 0;
        for (let n = 1; n <= MAX_CELL_LINE && padded < want; n += 1) {
          if (taken.has(n)) continue;
          padded += 1;
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
      this.restoreQueuedNotes();
      this.flushQueue();
      window.addEventListener("online", () => this.flushQueue());
      // Best effort for a note still being typed: the write-ahead already
      // protects the text, this only gets it to the server sooner.
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") this.noteBlur();
      });
      window.addEventListener("pagehide", () => this.noteBlur());
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
        // A coach cue has taken this number since the line was queued: no
        // input to show it on. The flush still sends it and the server's 422
        // drops it, rather than leaving an editable input over the cue.
        if (this.coachLineSet(ex).has(line)) continue;
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
        // Its text is on the line now, as this page's own.
        if (item.id) this._ownEntries[item.id] = true;
      }
    },

    // The same for the session note (see `restoreQueuedLines`): the server
    // renders the note it last saved, which is older than the one typed
    // offline. Show the queued text; `flushQueue` then sends it.
    restoreQueuedNotes() {
      const item = this.queuedNotes();
      if (!item) return;
      this.notes = item.body.notes;
      this.noteStatus = "queued";
      if (item.id) this._ownEntries[item.id] = true;
    },

    // ---- derived progress ----
    // The server's count of sets logged vs prescribed (`progress` in the page
    // data, and in every cell-write and log response). Anything that isn't two
    // non-negative integers leaves the current count alone.
    //
    // Saves of different lines can be in flight at once, so their responses
    // can land out of order. The server stamps each count with `as_of`
    // (microseconds since the epoch, taken just before its reads); a larger
    // one reflects at least every commit a smaller one saw, so a payload older
    // than the last one applied is ignored. A payload without `as_of` (an
    // older server mid-deploy) applies as it always did.
    applyProgress(p) {
      if (
        p &&
        typeof p === "object" &&
        Number.isInteger(p.logged) &&
        Number.isInteger(p.prescribed) &&
        p.logged >= 0 &&
        p.prescribed >= 0
      ) {
        const stamped = Number.isFinite(p.as_of);
        if (stamped && p.as_of < this._progressAsOf) return;
        this.progress = { logged: p.logged, prescribed: p.prescribed };
        if (stamped) this._progressAsOf = p.as_of;
      }
    },

    // "3 of 4 sets logged" — or "5 sets logged" when nothing is prescribed.
    // The server renders the same string for first paint
    // (`presenters.progress_label`); the two must stay identical.
    get progressLabel() {
      const { logged, prescribed } = this.progress;
      if (prescribed > 0) {
        const noun = prescribed === 1 ? " set" : " sets";
        return logged + " of " + prescribed + noun + " logged";
      }
      return logged + (logged === 1 ? " set" : " sets") + " logged";
    },

    // Complete the session: POST `{status: "done"}` to the log endpoint. The
    // sets themselves were saved line by line as the athlete typed; this only
    // stamps the status. When the network is unreachable (flaky gym wifi — S7),
    // the request is stashed locally and flushed on reconnect instead of being
    // lost; an HTTP error (the server answered) is a real error the athlete
    // should retry.
    async finish() {
      if (this.saving || !this.logUrl) return;
      this.saving = true;
      this.saved = false;
      this.error = false;
      this.queued = false;
      this.lineError = false;
      // #570: what to put back if this request is REFUSED (a non-retryable
      // response, below) rather than merely delayed — the database kept
      // nothing, so the optimistic flip right below has to come back off
      // too, and it can only do that if the value it's overwriting was
      // captured first.
      const previousStatus = this.status;
      // Reflect the intended status locally right away so the UI is responsive
      // whether the request lands now or after a sync.
      this.status = "done";
      const payload = { status: "done" };
      // Written ahead, like a line (#527): the wait below can take a while on
      // bad wifi, and leaving the page meanwhile must not lose the finish. The
      // flush leaves this entry to finish(), which takes it out once it lands.
      // The payload is a constant, so settling the lines below can't change
      // it: there is no second, rebuilt copy to enqueue.
      const ahead = this.enqueue(payload);
      // The note first of all: a note typed just before Finish is still
      // debouncing or in flight, and the log must not land before or over it.
      // (It stays a separate request — `payload` is unchanged so an older
      // server never sees a key it doesn't know — and its outbox entry is
      // merged with `ahead`, so neither erases the other.)
      try {
        await this.settleNotes();
      } catch (err) {
        console.error("Could not settle the note before finishing", err);
      }
      // Lines next (#527). Pressing the button blurs the line being typed, so
      // its save is already on its way: let it land, then send any line still
      // queued from earlier. The log then reaches the server after the sets its
      // lines carry, one request at a time, and the count it reports below
      // covers the lines too.
      try {
        await this.settleLines();
      } catch (err) {
        // Never let the outbox keep the log itself from being saved.
        console.error("Could not settle the lines before finishing", err);
      }
      let res;
      try {
        res = await postJson(this.logUrl, payload, this.csrf);
      } catch (netErr) {
        // Network unreachable → queue it; the upsert endpoint is idempotent, so
        // replaying on reconnect is safe (latest write for a session wins).
        if (this.keepForLater(payload) || this.holdsThisLog(ahead)) {
          this.statusBeforeQueued = previousStatus;
        } else {
          this.status = previousStatus;
        }
        this.saving = false;
        return;
      }
      try {
        // A redirect means the session expired and we were bounced to login —
        // the write never reached the endpoint (res.ok is true for the login
        // HTML). Don't lose it: queue for retry, where the next online flush
        // (after re-login) carries a fresh CSRF.
        if (res.redirected) {
          if (this.keepForLater(payload) || this.holdsThisLog(ahead)) {
            this.statusBeforeQueued = previousStatus;
          } else {
            this.status = previousStatus;
          }
          return;
        }
        // The Finish button hides once the status is "done", so every failure
        // below must leave either a queued retry or a visible button — never
        // neither.
        if (!res.ok) {
          if (isRetryableStatus(res.status)) {
            // The server failed (5xx/408/429), not the write: it might yet
            // land, so treat it like a network failure — keep the entry for
            // the next flush (`flushLog` also reads these as "kept"). Dropping
            // it here would leave "Logged" up with nothing to retry it.
            if (this.keepForLater(payload) || this.holdsThisLog(ahead)) {
              this.statusBeforeQueued = previousStatus;
            } else {
              this.status = previousStatus;
            }
            return;
          }
          // #570: a refusal is deterministic — retrying the same payload can
          // only fail again — so the optimistic "done" above comes back off
          // (the database kept nothing), the button returns, and the athlete
          // retries by hand.
          this.status = previousStatus;
          throw new Error("Request failed: " + res.status);
        }
        let data;
        try {
          data = await res.json();
          if (!data || !data.log || typeof data.log.status !== "string") {
            throw new Error("unexpected reply shape");
          }
        } catch (e) {
          // A 200 we can't read is not proof the server stored the write: a
          // proxy can answer 200 with HTML or `{}`. Treat it like a network
          // failure and keep the entry. Replaying `{status: "done"}` is
          // idempotent (DONE is sticky, the date is kept), so a retry costs
          // nothing whether or not the first write landed.
          if (this.keepForLater(payload) || this.holdsThisLog(ahead)) {
            this.statusBeforeQueued = previousStatus;
          } else {
            this.status = previousStatus;
          }
          return;
        }
        // Only a reply we could read says the server has this write.
        if (ahead) this.settleLog(payload);
        this.status = data.log.status;
        this.statusBeforeQueued = ""; // the server has this write; nothing to put back
        this.applyProgress(data.progress);
        this.reportSaved();
        // Key the tour nudge off the log status the *server* persisted, not the
        // button pressed (#451): the self-variant "results" step advances on a
        // `done` log (`advance_self_step_if_complete("results")`). A pending
        // reply → no spurious re-render / screen-reader re-announcement (the
        // offline `flushQueue` path stays silent regardless — it never calls
        // this).
        if (data.log.status === "done") notifyTourRefresh();
      } catch (err) {
        console.error("Log save failed", err);
        this.error = true;
        // A refusal is the athlete's to retry, not the outbox's.
        if (ahead) this.settleLog(payload);
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
      // A refusal outranks a tick. A finish the server refused is still
      // refused — nothing retries it, since a refusal drops its outbox entry —
      // so "Saved ✓" would be a plain lie. The flush that brings us here may
      // well have landed a log queued by ANOTHER tab on the same session:
      // `flushedMine` means a log for this URL landed, not that this page's
      // did. `finish()` clears `error` at the top of the next real attempt,
      // which is the moment the claim stops being true.
      if (this.error) return;
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
      // A note still being typed has its write-ahead entry in the outbox too,
      // but that isn't "waiting for the network" — it's about to be sent.
      const noteBusy = !!this._noteTimer || this._notesRunning > 0;
      return (
        lineQueued ||
        this.readQueue().some(
          (i) =>
            !isCellEntry(i) &&
            i.url === this.logUrl &&
            this.isMine(i) &&
            !(noteBusy && Object.keys(i.body).join() === "notes"),
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

    // Only well-formed entries: anything else in the key (another script's
    // value, a hand edit) would otherwise throw deep inside a flush and leave
    // "Finish session" stuck.
    readQueue() {
      let items;
      try {
        items = JSON.parse(localStorage.getItem(this.queueKey) || "[]");
      } catch (e) {
        return [];
      }
      if (!Array.isArray(items)) return [];
      return items.filter((i) => i && typeof i === "object" && i.body);
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

    // Whether `entry` — the write-ahead copy `finish()` made before the request
    // went out — is still in the outbox for this session. `writeQueue` is
    // all-or-nothing, so a LATER `enqueue` of the same write can fail while
    // that earlier copy sits there perfectly intact and due to flush: the
    // write is queued, not lost, and saying "couldn't save" (or taking the
    // status back off) would under-claim what the page actually holds.
    //
    // By content, not id: a note queued after `entry` merges into it (see
    // `enqueue`), which gives the merged entry a new id while it still holds
    // everything `entry` did.
    holdsThisLog(entry) {
      if (!entry) return false;
      const held = this.queuedLog();
      return (
        !!held &&
        Object.keys(entry.body).every(
          (k) => JSON.stringify(held.body[k]) === JSON.stringify(entry.body[k]),
        )
      );
    },

    // The one outbox entry for this session's log that this page may send.
    queuedLog() {
      return this.readQueue().find(
        (i) => !isCellEntry(i) && i.url === this.logUrl && this.isMine(i),
      );
    },

    // The queued entry carrying this session's note, if any.
    queuedNotes() {
      const item = this.queuedLog();
      return item && typeof item.body.notes === "string" ? item : null;
    },

    // True when the outbox took it. False means storage refused (a full or
    // blocked store), and then NOTHING holds this save — not the server, not
    // the queue — so the caller must not leave the optimistic "done" badge up
    // (#570, round 2): "Logged" with no log anywhere and no retry pending is
    // the worst version of the claim this whole slice exists to stop.
    keepForLater(payload) {
      if (this.enqueue(payload)) {
        this.queued = true;
        return true;
      }
      this.error = true;
      return false;
    },

    // Returns the entry as stored, or null when storage refused it.
    //
    // The log url still holds ONE entry, but a new write is MERGED over the
    // one it replaces rather than replacing it whole: the log carries two
    // independent things (`status` from Finish, `notes` from the session note),
    // and a Finish queued after an undelivered note must not erase the note, nor
    // a note the Finish. Per key the latest value wins, and each key lives
    // until it is delivered (`settleLog`). Only this athlete's entry is
    // merged; another's is dropped as it always was.
    enqueue(payload) {
      const all = this.readQueue();
      const same = (i) => !isCellEntry(i) && i.url === this.logUrl;
      const earlier = all.filter(same).filter((i) => this.isMine(i));
      // Only the two keys the log endpoint knows to deliver independently are
      // carried over: anything else in an old entry (a queue written before
      // typed lines, with its `sets`) is superseded as it always was.
      const carried = earlier.map((i) => {
        const kept = {};
        for (const key of ["status", "notes"]) {
          if (key in i.body) kept[key] = i.body[key];
        }
        return kept;
      });
      const body = Object.assign({}, ...carried, payload);
      const queue = all.filter((item) => item.url !== this.logUrl);
      const item = this.stamp({ url: this.logUrl, body });
      queue.push(item);
      return this.writeQueue(queue) ? item : null;
    },

    // Write the note ahead, like a line: the text is in the outbox before any
    // request, so closing the page can't lose it. Cheap (one localStorage write).
    enqueueNotes(text) {
      const item = this.enqueue({ notes: text });
      if (item) this._ownEntries[item.id] = true;
      return item;
    },

    // Take what was delivered out of this session's log entry — key by key, not
    // the entry whole. A merged entry can carry a note AND a status, and the
    // request that landed carried only one of them; and a key rewritten while
    // the request was in flight (the athlete kept typing) holds a different
    // value now and stays. An entry left with no keys is removed. `sent` is the
    // body as it went out.
    settleLog(sent) {
      const queue = this.readQueue();
      const index = queue.findIndex(
        (i) => !isCellEntry(i) && i.url === this.logUrl && this.isMine(i),
      );
      if (index === -1) return;
      const body = { ...queue[index].body };
      let changed = false;
      for (const key of Object.keys(sent)) {
        if (key in body && JSON.stringify(body[key]) === JSON.stringify(sent[key])) {
          delete body[key];
          changed = true;
        }
      }
      if (!changed) return;
      if (Object.keys(body).length) {
        queue[index] = { ...queue[index], body };
      } else {
        queue.splice(index, 1);
      }
      this.writeQueue(queue);
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
      if (!this.writeQueue(queue)) return null;
      this._ownEntries[item.id] = true;
      return item;
    },

    queuedCell(exerciseId, line) {
      return this.readQueue().find((item) =>
        isSameCell(item, this.cellUrl, exerciseId, line),
      );
    },

    isQueued(item) {
      const key = JSON.stringify(item);
      return this.readQueue().some((queued) => JSON.stringify(queued) === key);
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

    // Replay the outbox. `init()`, `online` and `finish()` can all ask at once,
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
        // Mid-finish, this session's log is finish()'s to send, right after.
        if (this.saving && item.url === this.logUrl) continue;
        // The lines before it can take a while: a log sent or replaced since
        // this pass read the outbox (finish() landing meanwhile) is not resent,
        // or its older copy would replace the newer log on the server.
        if (!this.isQueued(item)) continue;
        const outcome = await this.flushLog(item);
        if (outcome === "offline") break;
        if (outcome === "mine") flushedMine = true;
      }
      // A pass that lands this session's log, or the last line a "will sync"
      // message was waiting on, updates the message. Not mid-`finish()`, though:
      // finish reports once its own log is in.
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
        res = await postJson(
          item.url,
          item.owner ? { ...item.body, owner: item.owner } : item.body,
          this.csrf,
        );
      } catch (netErr) {
        return "offline";
      }
      if (isWrongAccount(res)) return "offline";
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
      // This session's note goes through the note's own chain, so a replay
      // can't race (or overtake) the debounced save of what's in the textarea:
      // both would otherwise be in flight at once, and the older text could land
      // last. What is left of the entry afterwards — a Finish queued alongside —
      // is sent below, without the note.
      let body = item.body;
      if (item.url === this.logUrl && typeof body.notes === "string") {
        const outcome = await this.saveNotes();
        if (outcome === "offline" || outcome === "kept") return outcome;
        const rest = this.queuedLog();
        body = rest ? { ...rest.body } : {};
        delete body.notes;
        if (!Object.keys(body).length) return outcome === "saved" ? "mine" : "saved";
        item = { ...item, body };
      }
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
      // BEFORE the refusal split below, and load-bearing: `isWrongAccount`
      // covers 403 and 409, which say "not postable as this account right
      // now" — a rotated CSRF token after a re-login (this page captures
      // `csrf` once, at load), or a write belonging to someone else. They are
      // not refusals of the payload, and dropping one would destroy the only
      // copy of a session finished offline. `flushCell` makes this check
      // first for the same reason.
      if (isWrongAccount(res)) return "offline";
      if (isRetryableStatus(res.status)) return "kept";
      if (!res.ok) {
        // A refusal won't change on retry, so keeping it queued promises a
        // sync that can never happen: the outbox re-POSTs the same doomed
        // payload on every `online` event while the footer says "will sync".
        // Drop it and say it failed instead — the same split `flushCell`
        // makes, which this function simply never had.
        //
        // Only for THIS session's log, for `flushCell`'s reason: another
        // session's log has nothing on this page to report it on, so it stays
        // queued and is refused again on its own page, where the athlete can
        // see it.
        if (item.url !== this.logUrl) return "kept";
        this.settleLog(item.body);
        // The badge goes back with it. `finish()` flipped `status` to "done"
        // optimistically before queuing this entry and recorded what it was
        // before (`statusBeforeQueued`); now that the server has refused the
        // entry and nothing is left to retry, leaving "Logged" up would claim
        // a log the database doesn't have. Only when we still know the
        // earlier value: an entry queued by a previous page load carries
        // none, and guessing would be worse than leaving the next page load
        // to say what the server holds.
        if (this.statusBeforeQueued) this.status = this.statusBeforeQueued;
        this.error = true;
        return "rejected";
      }
      // Read the body BEFORE taking the entry out: a 200 we can't read is not
      // proof the write landed (a proxy can answer 200 with HTML or `{}`), and
      // dropping the entry first would leave nothing to retry it. Keep it, for
      // this session's log and another's alike.
      let data;
      try {
        data = await res.json();
        if (!data || !data.log || typeof data.log.status !== "string") {
          throw new Error("unexpected reply shape");
        }
      } catch (e) {
        return "kept";
      }
      if (item.url === this.logUrl) {
        this.settleLog(item.body);
      } else {
        this.dropEntry(item);
        return "saved";
      }
      // A pass can already be sending this session's older log when finish()
      // starts, so its reply can land mid-finish — checked after the body is
      // read, which can itself outlast the tap. finish's own reply is the one
      // that reports, so leave the reconciling to it.
      if (this.saving) return "mine";
      // Only a write that carried a status gets to say what the status is.
      // What is left after a note's own replay has none (see above), but a
      // log entry from before notes existed always does.
      if ("status" in item.body) this.status = data.log.status;
      this.applyProgress(data.progress);
      return "mine";
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

    // Line numbers the coach's cues occupy on this exercise. The athlete's
    // stack never uses them.
    coachLineSet(ex) {
      return new Set(((ex && ex.coach_lines) || []).map((c) => c.line));
    },

    // The number a new athlete line would take: the smallest FREE (not
    // coach-occupied) number past the highest line the athlete has, or null
    // when none is left under the cap. Numbered off the max, not the length: a
    // sparse stack (the server dropped a cleared line but kept a later one)
    // would otherwise fabricate a duplicate number, breaking Alpine keys and
    // `saveCell` targeting.
    nextFreeLine(ex) {
      if (!ex) return null;
      const taken = this.coachLineSet(ex);
      const maxLine = (ex.sub_lines || []).reduce(
        (m, l) => Math.max(m, l.line || 0),
        0,
      );
      for (let n = maxLine + 1; n <= MAX_CELL_LINE; n += 1) {
        if (!taken.has(n)) return n;
      }
      return null;
    },

    // What an empty line shows. The server's own guess (the prescription as a
    // line, e.g. "225 x 5") wins; failing that a %1RM lift with a known load
    // can build one from the suggested load and the prescribed reps; failing
    // that, the generic hint.
    linePlaceholder(ex) {
      if (ex && ex.placeholder) return ex.placeholder;
      if (ex && ex.placeholder_reps && this.isPercentLift(ex)) {
        // The bare number: the unit is the plan's, so typing it is redundant,
        // and "135 x 5" is the shape the line parser is known to accept.
        const load = loadForPercent(this.effectiveOneRm(ex), this.percentTarget(ex));
        if (load != null) return fmtNum(load) + " x " + ex.placeholder_reps;
      }
      return "225 x 5, RPE 8 — or a note";
    },

    // Append an empty sub-line to the exercise's stack on the next free number,
    // up to MAX_CELL_LINE.
    addLine(ex) {
      if (!ex) return;
      if (!Array.isArray(ex.sub_lines)) ex.sub_lines = [];
      const line = this.nextFreeLine(ex);
      if (line == null) return;
      // Nothing is saved on a new line, so blurring it empty posts nothing.
      ex.sub_lines.push({ line, text: "", savedText: "" });
    },

    // ---- session note ("Notes for your coach") ----
    // One freeform note for the whole session, saved through the log endpoint
    // as `{notes}`. It never touches `status`: the note is editable after the
    // session is done, and a note post is not a Finish.

    // Every keystroke writes the text ahead to the outbox (cheap, and the only
    // thing that survives a closed tab); the POST itself waits for a pause.
    // The status stays blank while typing — "saved offline" there would be noise.
    noteInput() {
      this.noteStatus = "";
      this._notesDirty = true;
      this.enqueueNotes(this.notes);
      if (this._noteTimer) clearTimeout(this._noteTimer);
      this._noteTimer = setTimeout(() => {
        this._noteTimer = null;
        this.saveNotes();
      }, 900);
    },

    // Leaving the field (or the page) sends now, without waiting out the pause.
    noteBlur() {
      if (this._noteTimer) {
        clearTimeout(this._noteTimer);
        this._noteTimer = null;
      }
      return this.saveNotes();
    },

    // Let a note still debouncing or in flight land, so a Finish that follows
    // reaches the server after it. A failed save leaves the note queued; Finish
    // goes ahead regardless.
    async settleNotes() {
      if (this._noteTimer) {
        clearTimeout(this._noteTimer);
        this._noteTimer = null;
      }
      await this._notesSave.catch(() => {});
      if (this.notes !== this._notesSavedText || this.queuedNotes()) {
        await this.saveNotes();
      }
    },

    // Serialize note POSTs, like `saveCell`: two in flight could land out of
    // order and leave the older text. The text is read when the request goes
    // out, so edits made while an earlier one runs coalesce into the latest.
    // Resolves to "saved", "offline", "kept", "rejected" or "skipped".
    saveNotes() {
      if (!this.logUrl) return Promise.resolve("skipped");
      this._notesRunning += 1;
      const run = this._notesSave
        .catch(() => {})
        .then(() => this._postNotes())
        .finally(() => {
          this._notesRunning -= 1;
        });
      this._notesSave = run;
      return run;
    },

    async _postNotes() {
      const queued = this.queuedNotes();
      // A note ANOTHER tab wrote ahead is newer than this tab's untouched
      // textarea: sending the textarea would overwrite it with the text this
      // tab loaded with (a stale tab replaying a shared entry, or finishing).
      // Only text typed in THIS tab (`_notesDirty`) outranks the queued one.
      if (queued && !this._notesDirty && queued.body.notes !== this.notes) {
        this.notes = queued.body.notes;
      }
      const text = this.notes;
      // The server already has this text and nothing is waiting to say otherwise.
      if (text === this._notesSavedText && !queued) return "skipped";
      // Written ahead, as the input handler does — covers a send that doesn't
      // start from typing (a Finish, a replay whose entry was since replaced).
      let held = !!queued && queued.body.notes === text;
      if (!held) held = !!this.enqueueNotes(text);
      const body = { notes: text };
      const keep = () => {
        // Storage refused the write-ahead: nothing will sync it.
        this.noteStatus = held ? "queued" : "error";
      };
      let res;
      try {
        res = await postJson(this.logUrl, body, this.csrf);
      } catch (netErr) {
        keep();
        return "offline";
      }
      if (res.redirected || isWrongAccount(res)) {
        keep();
        return "offline";
      }
      if (isRetryableStatus(res.status)) {
        keep();
        return "kept";
      }
      if (!res.ok) {
        // Refused for good (too long, say): retrying the same text can only
        // fail again, so it leaves the outbox and the athlete sees it failed.
        this.settleLog(body);
        this._notesDirty = this.notes !== text;
        this.noteStatus = "error";
        return "rejected";
      }
      let data;
      try {
        data = await res.json();
        if (!data || typeof data.log !== "object" || data.log === null) {
          throw new Error("unexpected reply shape");
        }
      } catch (e) {
        // A 200 we can't read isn't proof the note landed; keep the entry.
        keep();
        return "kept";
      }
      // Only the keys as sent: a Finish queued alongside stays, and so does
      // text typed since (its key holds a different value now).
      this.settleLog(body);
      this._notesSavedText = text;
      this._notesDirty = this.notes !== text;
      // Deliberately NOT `data.log.status`: the reply carries the log's
      // status, but a note post must never move the badge (a Finish may be
      // queued separately while the server still says pending).
      this.applyProgress(data.progress);
      if (this.notes === text) {
        this.noteStatus = "saved";
        setTimeout(() => {
          if (this.noteStatus === "saved") this.noteStatus = "";
        }, 2400);
      } else {
        this.noteStatus = "";
      }
      return "saved";
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
      const key = ex.id + ":" + line;
      if (!fromQueue) {
        this._blurSaves += 1;
        this._writeAheadOnBlur(ex, line, key);
      }
      this._lineSavesRunning[key] = (this._lineSavesRunning[key] || 0) + 1;
      const previous = this._cellSaves[key] || Promise.resolve();
      const run = previous
        .catch(() => {}) // a failed save must not stall the cell's queue
        .then(() => this._postCell(ex, line, fromQueue))
        .finally(() => {
          this._lineSavesRunning[key] -= 1;
        })
        .then((outcome) => {
          // A line landing can change what the footer last said: once no
          // line is queued or refused, "will sync" or "a line couldn't save"
          // gives way — no second "Finish session" needed — and a line that
          // failed after "Saved ✓" went up takes it down. `finish()` reports
          // for itself, after its own log.
          if (!this.saving && (this.queued || this.lineError || this.saved)) {
            this.reportSaved();
          }
          return outcome;
        });
      this._cellSaves[key] = run;
      return run;
    },

    // Queue the line at the blur itself, not only when its save's turn comes:
    // a save for the same line can be in flight for up to 15s on bad wifi,
    // and closing the page meanwhile must not lose what was just typed. When
    // this save runs it queues the text again (replacing this entry) and sends
    // it. Only a line that has something to send — or one whose earlier save
    // is still running, since the server may yet end up with that one.
    _writeAheadOnBlur(ex, line, key) {
      const entry = (ex.sub_lines || []).find((l) => l.line === line);
      if (!entry) return;
      const text = entry.text || "";
      const busy = (this._lineSavesRunning[key] || 0) > 0;
      if (!busy && !this._lineNeedsSending(entry, text, ex.id, line)) return;
      this.enqueueCell({ exercise_id: ex.id, line, text });
    },

    // The dirty check (#527): a line whose text the server already has needs
    // no POST — tabbing through a blank line or leaving a coach's line as it
    // was changes nothing, and offline it painted a spurious "couldn't save".
    // A queued line always posts — its text may match, but the queue must
    // drain. So does a warned one: set-shaped text saved while its row was
    // skipped has no set, and re-sending the same text once the coach
    // un-skips the row is how it gets one. `savedText` is undefined when
    // unknown (a response that couldn't be read or had gone stale), and then
    // the line always posts.
    //
    // ...but not EVERY warned one (#572). A tint says the line has no set
    // backing it HERE, and the repost above is the repair only when the reason
    // is that no set exists at all. `warn_reason === "elsewhere"` means the
    // opposite: the athlete already logged this performance, on the day the
    // coach has since dragged the exercise off, and the cell travelled with the
    // `ExerciseSlot` while the `LoggedSet` stayed behind (#568's decision). For
    // that reason a repost isn't a repair — `_upsert_parsed_set` writes against
    // the NEW day's log and mints a SECOND row for one performance, with the
    // old one still counting toward results, 1RM and the agent's grounding. So
    // merely focusing and leaving such a line duplicated the set. Any other
    // reason — including "" from a server that doesn't send one yet, mid
    // rolling deploy — keeps the old behavior, which is the safe direction: a
    // needless repost is idempotent, a missing one loses the set.
    //
    // And a line this page has an entry queued for always posts: a blur made
    // while an earlier save of it ran queued text that hasn't been sent. The
    // line as it is now replaces it — or that older text would replay later,
    // over this.
    _lineNeedsSending(entry, text, exerciseId, line) {
      if (
        entry.savedText === undefined ||
        text !== entry.savedText ||
        entry.queued ||
        (entry.warn && entry.warn_reason !== "elsewhere")
      ) {
        return true;
      }
      const queued = this.queuedCell(exerciseId, line);
      return !!queued && !!this._ownEntries[queued.id];
    },

    // POST one exercise's sub-line cell. Blank text clears the cell in place
    // (the server never deletes a sub-line). Resolves to the outcome the flush
    // acts on:
    //
    //   "saved"    the server has this text.
    //   "offline"  the network is down, or the write never reached the
    //              endpoint as its own account (`isWrongAccount`). It stays
    //              queued (#527) and the line says it will sync.
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
        if (entry && !this._lineNeedsSending(entry, text, ex.id, line)) {
          entry.saveError = false;
          // The blur may have queued this very text while an earlier save
          // ran; that save has since put it on the server.
          const moot = this.queuedCell(ex.id, line);
          if (moot && moot.body.text === text) this.dropEntry(moot);
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
        res = await postJson(
          this.cellUrl,
          this.owner ? { ...body, owner: this.owner } : body,
          this.csrf,
        );
      } catch (netErr) {
        // It may have landed or not: what the server holds is unknown now.
        if (entry) entry.savedText = undefined;
        this._holdCell(entry, ex.id, line, !!sent);
        return "offline";
      }
      if (isWrongAccount(res)) {
        this._holdCell(entry, ex.id, line, !!sent);
        return "offline";
      }
      if (isRetryableStatus(res.status)) {
        if (entry) entry.savedText = undefined; // a 502/504 may have committed
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
        // the line's saved text is too: the next blur sends it again
        // (harmlessly) and reconciles.
        if (entry) entry.savedText = undefined;
        return "saved";
      }
      // The count is session-wide, not this line's, so it applies before the
      // stale-text checks below. Responses from two different cells can land
      // out of order; that is safe because `applyProgress` drops any payload
      // whose `as_of` is older than the one already shown.
      this.applyProgress(data.progress);
      // A replay of text another tab queued: if this tab never touched the
      // line, show what the server now holds, or a later blur here would post
      // the old text back over it. Never for this page's own entry — the line
      // may have been edited back to its saved text while the replay ran, and
      // that edit is the newer one.
      if (
        entry &&
        fromQueue &&
        sent &&
        sent.id &&
        !this._ownEntries[sent.id] &&
        entry.savedText !== undefined &&
        entry.text === entry.savedText
      ) {
        entry.text = text;
      }
      // Drop a stale response. Two saves for the same sub-line can be in
      // flight at once, and the older one can land last — so fixing `225 x`
      // to `225 x 5` could re-apply the first reply's warn and leave the cell
      // tinted for text that no longer exists. Only trust a reply whose text
      // is still what's in the input.
      // Derive-on-read warn (5a §8): re-classified server-side from the
      // just-committed text, so fixing a fat-fingered attempt (or typing one)
      // updates the cell's color right away, without a page reload.
      if (!entry) return "saved";
      if ((entry.text || "") !== text) {
        // The line changed while this was in flight, so what the server has
        // is no longer what it shows; the next blur sends it either way.
        entry.savedText = undefined;
        return "saved";
      }
      entry.savedText = text;
      entry.warn = !!(data.cell && data.cell.warn);
      // WHY it's tinted, not just whether (#572) — `_lineNeedsSending` treats
      // one reason as a repair to re-post and one as a duplicate to leave
      // alone. Derive-on-read like `warn` itself, so a reason that stops
      // applying clears on the next blur. "" when the server sent none.
      entry.warn_reason = (data.cell && data.cell.warn_reason) || "";
      // Optimistic PR (5a §7), marked ON THE LINE THAT EARNED IT: a blur
      // happens wherever the athlete is typing, and UAT found a page-top
      // celebration firing off-screen every time. The point of the optimistic
      // path is feedback in the moment, so it belongs beside the cell, in the
      // same slot as this line's other status labels.
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
