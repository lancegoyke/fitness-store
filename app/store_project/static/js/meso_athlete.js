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

// Issue #567: `athlete_log_session` used to decide what a posted Set row
// MEANT by matching `(prescription, set_number, values)` — but a hidden
// parsed row isn't on screen, so the client's `set_number` is only evidence
// about a render that can be several saves stale, and the slot's own meaning
// can have moved (a renumbering pass shifts a hidden row off the number this
// page still shows) since this page last loaded. The fix is row identity in
// the payload: a row that already has a server id posts that id; a row that
// doesn't mints its own id CLIENT-SIDE so the server can tell "this is the
// same not-yet-created row, retried" from "this is a second, genuinely new
// performance" — something position alone can never say. `crypto.randomUUID`
// covers every current browser (and this file's own test environment); the
// fallback (an insecure context, or the Safari that shipped without it) only
// has to be unique within one page's lifetime, so a counter salted with
// `Math.random()` is enough. Kept well under the server's 64-char cap.
let _clientIdSeq = 0;
function newClientId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  _clientIdSeq += 1;
  return "c" + _clientIdSeq.toString(36) + Math.random().toString(36).slice(2, 10);
}

// #567/#568 P1-I: does `item` (a response set) carry what `posted` (the set
// object THIS grid row actually posted, from the payload being reconciled —
// see `syncFromLog`) says it posted? Only `reps`/`load`/`rpe` are compared —
// never `id`/`client_id`/`prescription`/`set_number`, which is exactly what
// the caller used to FIND `item` in the first place and so is already
// settled by the time this runs. A missing field on either side defaults to
// `""`, since a hand-built response stub (or a genuinely blank set) omits
// fields the same way an empty string would compare. Sound because the
// server (`_clean_logged_sets`) stores these three fields verbatim, with no
// normalisation — a row the server created FOR a posted set always echoes
// back byte-identical values, while a row that merely happens to sit at the
// same slot or share the same id essentially never does.
function postedValuesMatch(item, posted) {
  if (!item || !posted) return false;
  return (
    (item.reps ?? "") === (posted.reps ?? "") &&
    (item.load ?? "") === (posted.load ?? "") &&
    (item.rpe ?? "") === (posted.rpe ?? "")
  );
}

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

// #570: a non-retryable refusal (`athlete_log_session`'s 400, "Too many sets
// logged for Box Squat.") names the exercise, and the generic "try again"
// banner would tell the athlete to retry a save that can never succeed — so
// the server's own message has to reach them.
//
// Only a JSON body carrying an `error` string qualifies, which is the shape
// that endpoint uses for exactly one refusal. Its OTHER 400s are bare
// `HttpResponseBadRequest`s whose text is developer-facing ("Duplicate id in
// sets.", "status must be 'pending' or 'done'."): reading the body as plain
// text would put those in front of an athlete who can do nothing about them,
// and would also render whatever a proxy or load balancer answered with.
// Opting in per-message beats guessing from the body's shape. Truncated
// rather than dropped when long — an exercise name can be 255 characters, and
// a clipped message still names the lift, where no message at all doesn't.
async function readErrorMessage(res) {
  let data;
  try {
    data = await res.json();
  } catch (e) {
    return "";
  }
  // `ok: false` as well as `error`, because a proxy or WAF can answer a 4xx
  // with JSON of its own (`{"error": "Forbidden"}`) and this value is shown
  // to the athlete verbatim. Both keys together are this endpoint's shape,
  // not a generic one.
  const named =
    data && data.ok === false && typeof data.error === "string"
      ? data.error.trim()
      : "";
  return named.slice(0, 200);
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
    // #570: the refusal's own message ("Too many sets logged for Box
    // Squat."), when the last save's `error` came from a non-retryable
    // response that named one — blank otherwise, in which case the template
    // falls back to the generic banner text.
    errorMessage: "",
    queued: false, // a save is stashed locally, waiting for the network
    lineError: false, // the log landed, but a line the server refused didn't
    newRecords: [], // PRs the last save beat (Phase 4c) — the celebration toast
    _oneRmTimers: {}, // per-exercise debounce handles for the manual-1RM POST
    _cellSaves: {}, // per-cell promise chain, so blurs reach the server in order
    _blurSaves: 0, // line saves started by a blur, for `settleLines`
    _lineSavesRunning: {}, // per cell: saves chained and not yet finished
    _ownEntries: {}, // ids of the outbox entries this page wrote or restored
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
      // Issue #567: normalize every rendered row's server identity ONCE,
      // here, so the rest of the file never has to guess between `undefined`
      // (a field the injected JSON happened to omit) and `null` (a row the
      // server explicitly has no LoggedSet for): `r.id` is the LoggedSet pk
      // this grid row is bound to right now, or `null`. `r.client_id` always
      // starts `null` — it's minted lazily, once, the first time a row with
      // no id is actually sent (see `buildPayload`), not here, since most
      // rows already have a server id and never need one at all. (Alpine
      // proxies these objects once mounted, so plain assignment is fine —
      // this runs before that happens anyway.)
      for (const ex of this.exercises) {
        if (!Array.isArray(ex.set_rows)) ex.set_rows = [];
        for (const r of ex.set_rows) {
          r.id = r.id == null ? null : r.id;
          r.client_id = null;
        }
      }
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
        // Its text is on the line now, as this page's own.
        if (item.id) this._ownEntries[item.id] = true;
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
          const set = {
            prescription: e.id,
            set_number: r.set_number,
            reps: r.reps || "",
            load: r.load || "",
            rpe: r.rpe || "",
          };
          // Issue #567: identity, not position, is what the server matches a
          // posted row against — see the comment above `newClientId`. A row
          // that already has a server id posts that id; a row that doesn't
          // mints its OWN client_id the first time it's ever sent and
          // REMEMBERS it on the row (assignment sticks whether or not Alpine
          // has proxied it yet) — never both. That memoized id is what keeps
          // three different sends of the same never-saved row — the click-
          // time `enqueue`, the actual `fetch`, and any offline replay of
          // either — naming the SAME row instead of minting a fresh one each
          // time: `save()` calls `buildPayload` twice (once before
          // `settleLines()`, once after), and the queued copy can outlive
          // both if the page dies mid-request.
          if (r.id != null) {
            set.id = r.id;
          } else {
            if (!r.client_id) r.client_id = newClientId();
            set.client_id = r.client_id;
          }
          sets.push(set);
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
      this.errorMessage = "";
      this.queued = false;
      this.lineError = false;
      this.newRecords = []; // clear any prior toast; this save recomputes it
      // #570: what to put back if this save is REFUSED (a non-retryable
      // response, below) rather than merely delayed — the database kept
      // nothing, so the optimistic flip right below has to come back off
      // too, and it can only do that if the value it's overwriting was
      // captured first.
      const previousStatus = this.status;
      // Reflect the intended status locally right away so the UI is responsive
      // whether the request lands now or after a sync.
      if (markDone) this.status = "done";
      // Written ahead, like a line (#527): the wait below can take a while on
      // bad wifi, and leaving the page meanwhile must not lose the Set rows.
      // The flush leaves this entry to save(), which replaces it with what it
      // finally sends, and takes that out once it lands.
      const ahead = this.enqueue(this.buildPayload(markDone));
      // Lines first (#527). Pressing the button blurs the line being typed, so
      // its save is already on its way: let it land, then send any line still
      // queued from earlier. The log then reaches the server after the sets its
      // lines carry, one request at a time, and what this save reports below
      // covers the lines too.
      try {
        await this.settleLines();
      } catch (err) {
        // Never let the outbox keep the log itself from being saved.
        console.error("Could not settle the lines before saving", err);
      }
      // Built after the wait, which can take a while on bad wifi: the Set rows
      // stay editable meanwhile, and a change made then belongs in this save.
      const payload = this.buildPayload(markDone);
      // And queued in place of the click-time copy before it goes out: if the
      // page dies mid-request, this newer log is the one to replay. (Storage
      // refusing it leaves the click-time copy, the best there is.)
      const sending = this.enqueue(payload) || ahead;
      let res;
      try {
        res = await postJson(this.logUrl, payload, this.csrf);
      } catch (netErr) {
        // Network unreachable → queue it; the upsert endpoint is idempotent, so
        // replaying on reconnect is safe (latest save for a session wins).
        if (!this.keepForLater(payload) && !this.holdsThisLog(sending)) {
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
          if (!this.keepForLater(payload) && !this.holdsThisLog(sending)) {
            this.status = previousStatus;
          }
          return;
        }
        if (!res.ok) {
          if (!isRetryableStatus(res.status)) {
            // #570: a refusal (e.g. "Too many sets logged for Box Squat.")
            // is deterministic — retrying the same payload can only fail
            // again — so the optimistic "done" above comes back off (the
            // database kept nothing) and the server's own message, naming
            // what actually went wrong, replaces the generic banner text.
            // A retryable status (5xx/408/429) falls through untouched: the
            // write might yet land, so neither the status nor the message
            // changes here — same as a network failure above.
            // Message first, THEN the revert: the body read is bounded by
            // `postJson`'s own timeout, and flipping the status back before
            // it resolves would leave the page showing neither "Logged" nor
            // a reason for up to that long.
            this.errorMessage = await readErrorMessage(res);
            this.status = previousStatus;
          }
          throw new Error("Request failed: " + res.status);
        }
        if (sending) this.dropEntry(sending);
        const data = await res.json();
        this.status = data.log.status;
        // `payload` — not `sending`'s body-only shape, though they carry the
        // same `sets` here — is this save's own request body, exactly what
        // was actually posted (#567/#568 P1-E/F): `syncFromLog` needs it to
        // tell a posted row from one this save never touched.
        this.syncFromLog(data.log, payload);
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
        // An HTTP error is the athlete's to retry, not the outbox's.
        if (sending) this.dropEntry(sending);
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
      // A refusal outranks a tick. An earlier save of THIS page's grid that
      // the server refused is still refused — nothing retries it, since a
      // refusal drops its outbox entry — so "Saved ✓" would be a plain lie,
      // and clearing the refusal to make room for the tick (which an earlier
      // version of this did) states it even more confidently. The flush that
      // brings us here may well have landed a log queued by ANOTHER tab on
      // the same session: `flushedMine` means a log for this URL landed, not
      // that this page's did. `save()` clears both at the top of the next
      // real attempt, which is the moment the claim stops being true.
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

    // Only well-formed entries: anything else in the key (another script's
    // value, a hand edit) would otherwise throw deep inside a flush and leave
    // "Log session" stuck.
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

    // Queue this session's log and say so — or, when storage refused it, say
    // it didn't save.
    // Whether `entry` — the write-ahead copy `save()` made before the request
    // went out — is still in the outbox for this session. `writeQueue` is
    // all-or-nothing, so a LATER `enqueue` of the same save can fail while
    // that earlier copy sits there perfectly intact and due to flush: the
    // save is queued, not lost, and saying "couldn't save" (or taking the
    // status back off) would under-claim what the page actually holds.
    holdsThisLog(entry) {
      return !!entry && this.readQueue().some((item) => item.id === entry.id);
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
    enqueue(payload) {
      const queue = this.readQueue().filter((item) => item.url !== this.logUrl);
      const item = this.stamp({ url: this.logUrl, body: payload });
      queue.push(item);
      return this.writeQueue(queue) ? item : null;
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
        // Mid-save, this session's log is save()'s to send, right after.
        if (this.saving && item.url === this.logUrl) continue;
        // The lines before it can take a while: a log sent or replaced since
        // this pass read the outbox (save() landing meanwhile) is not resent,
        // or its older copy would replace the newer log on the server.
        if (!this.isQueued(item)) continue;
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
      if (isRetryableStatus(res.status)) return "kept";
      if (!res.ok) {
        // A refusal (#570's 400) won't change on retry, so keeping it queued
        // promises a sync that can never happen: the outbox re-POSTs the same
        // doomed payload on every `online` event while the footer says "will
        // sync". Drop it and say what went wrong instead — the same split
        // `flushCell` makes, which this function simply never had.
        //
        // Only for THIS session's log, for `flushCell`'s reason: another
        // session's log has nothing on this page to report it on, so it stays
        // queued and is refused again on its own page, where the athlete can
        // see it.
        if (item.url !== this.logUrl) return "kept";
        this.dropEntry(item);
        this.error = true;
        this.errorMessage = await readErrorMessage(res);
        return "rejected";
      }
      this.dropEntry(item);
      if (item.url !== this.logUrl) return "saved";
      let data;
      try {
        data = await res.json();
      } catch (e) {
        return "mine"; // synced server-side regardless; UI reconciles on next load
      }
      // A pass can already be sending this session's older log when save()
      // starts, so its reply can land mid-save — checked after the body is
      // read, which can itself outlast the tap. Leave the rows alone then:
      // reconciling them to the older log would un-tick rows ticked since,
      // just before save() builds its payload from them. save's own reply
      // reconciles.
      if (this.saving) return "mine";
      try {
        this.status = data.log.status;
        // `item.body` is exactly what this queued entry posted (#567/#568
        // P1-E/F) — same contract as `save()`'s own call above.
        this.syncFromLog(data.log, item.body);
        this.newRecords = data.new_records || []; // a PR beaten offline still lands
      } catch (e) {
        /* a reply of an unexpected shape: synced server-side regardless */
      }
      return "mine";
    },

    // Reconcile the rows with what the server actually persisted so the check
    // circles and counter match the saved log immediately — without this, rows
    // that were sent because they carried data (but were never ticked) would
    // stay un-checked until a reload. The returned log is the source of truth.
    //
    // Issue #567: reconcile by IDENTITY first, position second — but ONLY for
    // a grid row THIS PAYLOAD actually posted. `payload` is the request body
    // this exact save sent (`save()` and `flushLog()` both thread through the
    // one they actually posted — see their own calls below), and is now part
    // of this method's contract, not an optional extra: a caller that cannot
    // supply one treats NOTHING as posted, the strict reading, rather than
    // silently treating everything as posted (which is the bug below).
    //
    // #567/#568 P1-E/F, THE ROOT CAUSE three independent reviewers traced
    // back here: the slot fallback used to run over EVERY grid row, whether
    // or not this payload posted it. A grid row this save left untouched
    // (empty, unticked) has no business adopting ANYTHING from the response
    // — but the response can still carry a set at that row's slot for a
    // reason that has nothing to do with this save at all: a hidden parsed
    // row a coach's rewrite just made VISIBLE, echoed back because it's
    // visible now, happening to sit at a slot this stale page's own empty
    // grid row shares. The old fallback ticked that grid row and PLANTED the
    // visible row's real pk onto it, though its inputs stayed empty — and the
    // very next ordinary edit into that same-looking-empty row then posted
    // the planted id, letting the server delete a real, distinct performance
    // this page never touched or even knew existed (see
    // `athlete_log_session`'s docstring for the exact sequence). It was also
    // simply unstable on its own terms: the spurious tick made `rowFilled`
    // re-post that blank-looking row on every subsequent save, and each of
    // those repeated posts renumbered the real survivor one `set_number`
    // higher.
    //
    // #567/#568 P1-I: the paragraph this replaces argued the only visible row
    // left at a POSTED slot, after the save, is the one the server created
    // FOR that exact posted set — but that is only true when the set was
    // CREATED. When a posted set is instead ABSORBED (the twin absorb in
    // `athlete_log_session`), nothing is created at that slot at all, and
    // `posted` there is RECOMPUTED after the absorb runs — so the slot never
    // enters `posted` in the first place, and the collision renumbering (which
    // only moves a row off a slot IN `posted`) never even looks at it. A
    // visible parsed row already sitting at that same slot, left over from
    // before this save, is therefore untouched — and it is a DIFFERENT row
    // than the one whose id this grid row posted.
    //
    // Concretely: a hidden row X and a visible row Y can both exist on one
    // exercise's rows, at different set numbers, after a coach rewrite/undo
    // cycle. A payload posting `{id: X.pk, set_number: 1, reps: "5", load:
    // "225"}` is absorbed by X — the absorb matches on pk and VALUES alone,
    // with no `set_number` agreement, so this works even though X's own
    // `set_number` is 2, not 1 — which drops slot 1 out of `posted` entirely.
    // Y, sitting at slot 1 the whole time, is left exactly where it was. The
    // response's item at slot 1 is therefore Y, not X: without a value check,
    // legs 2 and 3 below would plant Y's pk onto this grid row, and the very
    // next ordinary edit into it would post that pk and let the server delete
    // a performance the page never rendered.
    //
    // So the rule that actually holds is narrower than "the only row at a
    // posted slot is the one this save created for it": a grid row may only
    // adopt a response item that carries what THAT ROW ITSELF POSTED — see
    // `postedValuesMatch`, above `newClientId`. Sound because
    // `_clean_logged_sets` stores `reps`/`load`/`rpe` verbatim, with no
    // normalisation, so a row the server created FOR a posted set always
    // echoes back byte-identical values, while a row that merely happens to
    // share a slot or an id essentially never does. Leg 1 (`client_id`) needs
    // no such check: a `client_id` the server echoes is an exact,
    // server-minted identity for THIS request's set, so a value check there
    // adds no safety and only risks a false negative.
    //
    // This also closes a second, related gap in leg 2 (`byId`): without the
    // value check it bound a grid row to a response item at a DIFFERENT
    // `set_number` without noticing — after a spared row is renumbered away
    // by the collision pass, the page would show a tick at the row's OLD
    // number for a row that has since moved to a different one.
    //
    // Why the value check refuses that match is worth stating exactly,
    // because the obvious reason is the WRONG one: it is not that "the
    // renumbered row's values didn't change" — unchanged values would make
    // `postedValuesMatch` ACCEPT it. It is that a row can only be both
    // SPARED and renumbered when its values differ from what this payload
    // posted: `_client_held` deletes any visible row an anchored id restates
    // verbatim (see `athlete_log_session`), so a visible survivor is one the
    // payload did NOT restate, and a hidden survivor never reaches the
    // response at all. Either way there is nothing left at the old number
    // for this row's own posted values to match.
    //
    // Match order, for a grid row `r`, where "posted" means this payload's
    // OWN `sets` list actually named `r`'s current `(prescription,
    // set_number)` — and, for legs 2 and 3, that the matched item's
    // `reps`/`load`/`rpe` equal what THIS row posted there (`postedValuesMatch`):
    //   1. `r.client_id`, against the response's client_id map — the row the
    //      server just created FOR this grid row. No posted-gate or value
    //      check needed: the server only ever echoes a client_id it just
    //      minted FROM this same request's payload, so a match here is
    //      impossible unless this row really was posted, and the id is
    //      strictly stronger evidence than a value comparison could be.
    //   2. posted AND `r.id != null`, against the response's id map, values
    //      matching — exact identity, for a row that already had a server id.
    //   3. posted, against the slot map (first write wins, as before), values
    //      matching — the rolling-deploy fallback: an OLD server build
    //      doesn't know `client_id` and never echoes it, so leg 1 fails for a
    //      client_id row even though it truly was posted; matching ONLY by
    //      client_id then left such a row un-ticked forever — `rowFilled`
    //      drops an unticked, empty-looking row from the NEXT save's payload,
    //      and that save deletes the very row the old server just created
    //      for it.
    //      Leg 3 can still land on a FOREIGN row whose values happen to
    //      equal what this row posted — `athlete_log_session` itself calls
    //      two identical performances ("225 x 5" twice) an ordinary thing to
    //      do. That is harmless, and deliberately so: the collision
    //      renumbering leaves at most one VISIBLE row at a slot this payload
    //      posted, so the pk adopted there is exactly the one a reload would
    //      bind to this grid row showing these values. Adopting it agrees
    //      with the render rather than guessing against it.
    // A row with NO match — posted or not — gets `r.done = false` and
    // NEITHER `r.id` NOR `r.client_id` touched:
    //   * POSTED, nothing at the id/slot at all: the server ABSORBED it (it
    //     restated a row hidden from the logger, a twin the coach's rewrite
    //     created), so the response carries no set for it. Clearing its id
    //     here would make the NEXT save mint a fresh client_id and post it as
    //     a brand-new row — creating the very duplicate #567 exists to
    //     prevent. Keeping the id lets the server absorb it again next time,
    //     which is stable.
    //   * POSTED, an item sits at the id/slot but its VALUES differ (P1-I):
    //     that item is a FOREIGN row this save never touched, exactly the
    //     scenario above. Leaving the row un-ticked and its id untouched
    //     means the worst case is a stale tick lingering one save longer, not
    //     a stranger's pk getting planted here — a reload re-derives this
    //     row from `_set_rows`, which knows the truth.
    //   * UNPOSTED: this save never touched the row at all, so there is
    //     nothing here to reconcile it against, whatever the response
    //     happens to carry at its slot. A reload renders it properly from
    //     `_set_rows` (values AND `done`), which is the only thing that
    //     actually knows this row's true state.
    syncFromLog(log, payload) {
      const sets = log.sets || [];
      // #567/#568 P1-I: a Map from slot to the posted set OBJECT, not a Set
      // of slot keys — legs 2 and 3 below need the actual values this row
      // posted, not just proof that its slot was posted at all.
      const posted = new Map(
        (payload && Array.isArray(payload.sets) ? payload.sets : []).map(
          (s) => [`${s.prescription}:${s.set_number}`, s],
        ),
      );
      const byClientId = new Map();
      const byId = new Map();
      const bySlot = new Map();
      for (const s of sets) {
        // Only an item the server just minted FROM a client_id carries one
        // back (the contract: `client_id` is null on every other item,
        // including a pre-existing row posted by `id`) — so this map can
        // only ever match the one row that named it.
        if (s.client_id) byClientId.set(s.client_id, s);
        if (s.id != null) byId.set(s.id, s);
        // P3: the FIRST entry wins a slot, not the last. Two response items
        // can only share a slot when the server is on an old build that
        // ignores `client_id` (see above) — the first is exactly the row
        // that build created FOR this slot; a later item that happens to
        // report the same slot (a survivor absorbed into it, say) is not.
        const slotKey = `${s.prescription}:${s.set_number}`;
        if (!bySlot.has(slotKey)) bySlot.set(slotKey, s);
      }
      for (const e of this.exercises) {
        for (const r of e.set_rows) {
          const postedSet = posted.get(`${e.id}:${r.set_number}`);
          // `undefined` (not merely falsy `null`) whenever the posted-gate
          // fails, so `postedValuesMatch` — which requires both arguments —
          // rejects it the same way it rejects "no item found".
          const idItem = postedSet && r.id != null ? byId.get(r.id) : undefined;
          const slotItem = postedSet
            ? bySlot.get(`${e.id}:${r.set_number}`)
            : undefined;
          // #567/#568 P1-E/F/I: identity first, slot second — posted AND
          // value-matched only, for legs 2/3 — and NEVER for a row this
          // payload didn't post. See the comment above this method.
          const match =
            (r.client_id && byClientId.get(r.client_id)) ||
            (postedValuesMatch(idItem, postedSet) && idItem) ||
            (postedValuesMatch(slotItem, postedSet) && slotItem);
          r.done = !!match;
          if (match) {
            // P3: a response item can legitimately omit `id` (nothing to
            // report — see the server's `client_ids` comment in
            // `athlete_log_session`). Assigning `match.id` unguarded then set
            // `r.id` to `undefined`, which `r.id != null` reads as "no id" —
            // so the very next `buildPayload` minted a brand-new `client_id`
            // for a row the server already knows, instead of leaving `r.id`
            // as it was.
            const matchId = match.id ?? null;
            if (matchId != null) r.id = matchId;
            r.client_id = null;
          }
          // NO match, POSTED or not: leave r.id and r.client_id exactly as
          // they are — do NOT clear them. See the comment above this method
          // for the shapes this covers (absorbed, a foreign row at the same
          // id/slot, or simply untouched).
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
