/* Meso — strength-training program designer.
 *
 * A port of the Meso.dc.html Claude Design prototype to Alpine.js. The designer
 * view injects a serialized plan and init() hydrates the grid (program / weeks /
 * phases) from it, then edits autosave to the JSON API. The client-side fixtures
 * were retired in Phase 5 — the page always renders a real, DB-backed plan now
 * (the bare URL redirects to one). The agent column is live as of agent Phase 3:
 * the coach's message POSTs to the real proposal endpoint (api/plan/<id>/agent/),
 * the returned batch renders inline, and a link sends the coach to the review
 * gate. The agent only *proposes* — changes stay inert until applied at review,
 * so the chat never mutates the grid here. (The canned keyword intent engine it
 * replaced — a client-side matcher that edited the grid in place — is gone.)
 */
function createMeso() {
  return {
    // ---- design tokens (the prototype exposed these as editor props) ----
    accent: "Cobalt",
    theme: "Clinical",
    unit: "kg",

    // ---- ui state ----
    mode: "individual", // individual | group — set from the plan on load
    // Group identity (name / focus / members / folded flags) when the open plan
    // is a group's shared program; null for an individual plan. Hydrated by
    // init() from the injected plan's `group` payload (serialize_group_identity).
    group: null,
    // Individual identity (real athlete name / initials / goal / active
    // contraindications) for the left rail; null for a group plan. Hydrated by
    // init() from the injected plan's `athlete` payload (serialize_athlete_identity).
    // Replaces the prototype's hardcoded "Maya Okonkwo" chrome (Phase 5).
    athlete: null,
    view: "week", // week | block | athlete
    periodStyle: "timeline", // timeline | ladder | calendar
    inputText: "",
    agentTyping: false,
    delivered: false,
    checks: {},
    exSeq: 1,

    // ---- first-run coachmarks (first-time UX Phase 5) ----
    // Three dismissible region notes (week grid / agent / phone preview) orient a
    // first-time coach. They show until dismissed; the dismissal persists in
    // localStorage so a coach who waved one away never sees it again (no server
    // "seen" flag — the persistence lives client-side, like the athlete onboarding
    // chrome). The reactive map drives `x-show`; init() hydrates it from storage.
    coachmarkKeys: ["grid", "agent", "phone"],
    coachmarksDismissed: {},

    // Group mode only: the open per-athlete override editor (one shared row),
    // or null when closed. Holds the targeted row (`ex`), the group's members,
    // the selected member id, and an editable `draft` of their adjust.
    override: null,

    // Individual mode only: the open coach 1RM editor (one %1RM row), or null
    // when closed. Holds the targeted row (`ex`) and an editable `value`.
    oneRm: null,

    // The agent runs as a background job (Phase 4): POST kicks it off, then we
    // poll the batch's status until it lands. `agentTyping` stays true (the
    // "drafting…" indicator) for the whole poll.
    pollIntervalMs: 1500,
    pollMaxAttempts: 40,

    // ---- backend hydration (Phase 3) ----
    // init() flips `live` on and fills program/weeks/phases from the plan the
    // view injects. Without an injected plan nothing hydrates and no network
    // calls are made (the bare designer URL redirects to a real plan, Phase 5).
    live: false,
    planId: null,
    csrf: "",

    // The week whose grid `program` currently holds (the multi-week switcher can
    // point this away from the live `current` week). init() seeds it from the
    // injected plan's `viewing`; switchWeek/addWeek/setCurrentWeek update it.
    viewedWeekId: null,

    // The thread starts with a single orienting greeting. On load, init()
    // replaces it with the plan's persisted conversation (rebuilt server-side
    // from the proposal batches and injected as `meso-chat-thread`) when there
    // is one; an empty history keeps this greeting. Real agent turns are
    // appended live as the coach sends instructions.
    messages: [
      {
        id: 1,
        role: "agent",
        text: "Tell me how you'd like to adjust this plan — swap a lift, change a day's volume, progress loads, or add a deload. I'll propose changes for you to review before anything touches the program.",
      },
    ],

    // Hydrated by init() from the injected plan (program = current week's
    // sessions, weeks = its mesocycle's week strip, phases = the macrocycle).
    program: [],
    weeks: [],
    phases: [],

    // Each chip's label is sent verbatim as the agent instruction.
    chips: [
      { label: "Lower Day 2 volume" },
      { label: "Swap a knee-sensitive lift" },
      { label: "Progress from last block" },
      { label: "Add a deload week" },
    ],

    calDays: ["M", "T", "W", "T", "F", "S", "S"],
    sessionDays: [0, 2, 4],

    // ---- derived ----
    get isGroup() {
      return this.mode === "group";
    },
    get isIndividual() {
      return this.mode !== "group";
    },

    // ---- real plan headers (Phase 5) ----
    // The prototype hardcoded "Week 2 — Accumulation" / "Hypertrophy block" over
    // whatever plan was open; these derive the same chrome from the real plan.
    get currentWeek() {
      return this.weeks.find((w) => w.current) || this.weeks[0] || null;
    },
    // The week the grid is showing — the switcher can point it away from the
    // live (`current`) week. Falls back to the current week before a switch.
    get viewedWeek() {
      return (
        this.weeks.find((w) => w.id === this.viewedWeekId) ||
        this.currentWeek
      );
    },
    weekIsViewed(w) {
      return !!w && w.id === this.viewedWeekId;
    },
    // True when the viewed week is also the live (deliver-target) week — drives
    // whether the "Make current" affordance shows.
    get viewedIsCurrent() {
      const w = this.viewedWeek;
      return !!(w && w.current);
    },
    get currentPhase() {
      return (
        this.phases.find((p) => p.state === "current") || this.phases[0] || null
      );
    },
    // The top-bar cycle chip, e.g. "Hypertrophy · Wk 2 / 4" — for the viewed week.
    get cycleLabel() {
      const p = this.currentPhase;
      const w = this.viewedWeek;
      const phase = p ? p.name : "";
      const wk = w
        ? w.label + (this.weeks.length ? " / " + this.weeks.length : "")
        : "";
      return [phase, wk].filter(Boolean).join(" · ");
    },
    // The week-view heading for the viewed week, e.g. "Wk 2 — Accum".
    get weekHeading() {
      const w = this.viewedWeek;
      if (!w) return "This week";
      return w.phase ? w.label + " — " + w.phase : w.label;
    },
    // The block-view heading, e.g. "Hypertrophy — 4 wk mesocycle".
    get blockHeading() {
      const p = this.currentPhase;
      if (!p) return "Mesocycle";
      return p.name + " — " + p.weeks + " mesocycle";
    },

    // The top-bar "Deliver" link target — the deliver/confirm screen for this
    // plan, pinned to the week the coach is *viewing* (?week=) so "Deliver" sends
    // the week on screen rather than always the live one (the deliver screen lets
    // a coach send a built-ahead week without first making it current). Falls back
    // to the bare deliver URL with no plan (the bare designer redirects to a real
    // plan anyway).
    get deliverHref() {
      if (this.planId == null) return "/meso/deliver/";
      const base = `/meso/deliver/${this.planId}/`;
      return this.viewedWeekId != null
        ? `${base}?week=${this.viewedWeekId}`
        : base;
    },

    // ---- backend hydration + autosave (Phase 3) ----
    init() {
      // Hydrate dismissed coachmarks first, so it works even without a plan.
      this.loadCoachmarks();
      const el = document.getElementById("meso-plan-data");
      if (!el) return; // no plan injected → empty grid (bare URL redirects away)
      let data;
      try {
        data = JSON.parse(el.textContent);
      } catch (e) {
        console.error("Could not parse plan data", e);
        return;
      }
      this.live = true;
      this.planId = data.plan.id;
      if (data.plan.unit) this.unit = data.plan.unit;
      this.applyPlanData(data);
      // The real athlete identity for the individual left rail (null for groups).
      this.athlete = data.athlete || null;
      // A group shared program opens in Group mode and renders its real identity
      // (members / flags); an individual plan stays in Individual mode. The agent
      // can edit the shared program for the whole group OR diverge one member with
      // a per-athlete auto-adjust (groups agent Phases 1–2) — it grounds on the
      // members + their contraindications — so swap the greeting for a
      // group-appropriate one (hydrateThread keeps this when the plan has no
      // batches).
      this.group = data.group || null;
      if (this.group) {
        this.mode = "group";
        this.messages = [
          {
            id: 1,
            role: "agent",
            text: "This is the group's shared program — every member trains off it. Ask me to change it for the whole group, or to adjust one athlete (a swap, a load %, or a volume tweak just for them). I propose changes for you to review and honor every member's contraindications.",
          },
        ];
      }
      const csrfEl = document.getElementById("meso-csrf");
      this.csrf = csrfEl ? csrfEl.dataset.token : "";
      this.hydrateThread();
    },

    // Replace the greeting with the plan's persisted conversation when the view
    // injects one. The thread is rebuilt server-side from the proposal batches
    // (serialize_chat_thread) in the exact `messages` shape, so it drops in
    // without remapping. An empty history (no batches) keeps the greeting.
    hydrateThread() {
      const el = document.getElementById("meso-chat-thread");
      if (!el) return;
      let thread;
      try {
        thread = JSON.parse(el.textContent);
      } catch (e) {
        console.error("Could not parse chat thread", e);
        return;
      }
      if (!Array.isArray(thread) || !thread.length) return;
      this.messages = thread;
      // Land a restored thread on its latest turn, not the oldest message — a
      // long conversation would otherwise reload scrolled to the top.
      this.scrollThread();
      // If the last turn was still drafting at render time, drop that
      // placeholder and resume polling so a run that finishes after the page
      // loads updates the thread instead of leaving it stuck on the note.
      const last = thread[thread.length - 1];
      if (last && last.pollUrl) {
        this.messages.pop();
        this.resumeDrafting(last.pollUrl);
      }
    },

    // Resume polling a batch that was still drafting when the page loaded. The
    // typing indicator shows while we wait; pollBatch appends the resolved reply
    // (or an error / timeout note) when the batch lands.
    async resumeDrafting(pollUrl) {
      this.agentTyping = true;
      this.scrollThread();
      try {
        await this.pollBatch(pollUrl);
      } finally {
        this.agentTyping = false;
        this.scrollThread();
      }
    },

    async apiPost(url, body) {
      const res = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": this.csrf,
        },
        body: body == null ? null : JSON.stringify(body),
      });
      if (!res.ok) throw new Error("Request failed: " + res.status);
      return res.json();
    },

    // Autosave one edited row to its prescription. No-op until a plan is loaded.
    persistRow(ex) {
      if (!this.live || !ex || ex.id == null) return;
      return this.apiPost(`/meso/api/plan/${this.planId}/prescription/${ex.id}/`, {
        name: ex.name ?? "",
        sets: ex.sets ?? "",
        reps: ex.reps ?? "",
        load: ex.load ?? "",
        load_type: ex.load_type ?? "abs",
        rpe: ex.rpe ?? "",
        note: ex.note ?? "",
      }).catch((err) => console.error("Autosave failed", err));
    },

    // ---- per-athlete override editor (group mode) ----
    //
    // In Group mode every shared row can be adjusted per athlete. openOverride
    // opens an editor for one prescription; the coach picks a member and edits
    // their diff (swap / load% / volume / note), and saveOverride POSTs it to
    // the override endpoint. The reply recomputes the row's `adj` badge, which
    // we paint straight back onto the row. The editor pre-fills from the
    // member's stored adjust (`ex.adjusts`), so editing and clearing both work
    // off the real diffs the serializer injected.

    // The member's stored diff on this row as editable strings (blank if unset).
    overrideDraft(ex, memberId) {
      const found = (ex.adjusts || []).find((a) => a.id === memberId);
      return {
        swap: (found && found.swap) || "",
        load_pct: found && found.load_pct != null ? String(found.load_pct) : "",
        sets: (found && found.sets) || "",
        reps: (found && found.reps) || "",
        note: (found && found.note) || "",
      };
    },

    openOverride(ex) {
      if (!this.isGroup || !this.live) return;
      const members = (this.group && this.group.members) || [];
      if (!members.length) return;
      // Land on a member who already adjusts this row (the badge the coach
      // tapped) so a save edits the existing adjust rather than overwriting the
      // wrong athlete; fall back to the first member for a fresh adjust. Guard
      // against a stored adjust whose member has since left the roster.
      const adjusted = (ex.adjusts || []).find((a) =>
        members.some((m) => m.id === a.id),
      );
      const memberId = adjusted ? adjusted.id : members[0].id;
      this.override = {
        ex,
        members,
        memberId,
        draft: this.overrideDraft(ex, memberId),
        saving: false,
        error: "",
      };
    },

    selectOverrideMember(memberId) {
      if (!this.override) return;
      this.override.memberId = memberId;
      this.override.draft = this.overrideDraft(this.override.ex, memberId);
      this.override.error = "";
    },

    closeOverride() {
      // Don't dismiss mid-save: submitOverride keeps the editor open on failure
      // to surface a retry, which relies on `this.override` staying non-null for
      // the whole request. Escape / backdrop both route through here, so this is
      // the one place that guard belongs (the footer buttons are also disabled).
      if (this.override && this.override.saving) return;
      this.override = null;
    },

    // True when the selected member already has a stored adjust on the open row
    // (so the editor can offer "Clear").
    get overrideHasExisting() {
      if (!this.override) return false;
      return (this.override.ex.adjusts || []).some(
        (a) => a.id === this.override.memberId,
      );
    },

    // Parse the load% field to the endpoint's int | null. Blank → null (clear
    // that part); anything but a whole number in the model's 1–200 band is
    // rejected here so the badge never repaints off a server 400.
    parseOverrideLoadPct() {
      const raw = (this.override.draft.load_pct || "").trim();
      if (raw === "") return { ok: true, value: null };
      if (!/^[0-9]+$/.test(raw)) return { ok: false };
      const n = parseInt(raw, 10);
      if (n < 1 || n > 200) return { ok: false };
      return { ok: true, value: n };
    },

    async saveOverride() {
      if (!this.override || this.override.saving) return;
      const parsed = this.parseOverrideLoadPct();
      if (!parsed.ok) {
        this.override.error = "Load % must be a whole number from 1 to 200.";
        return;
      }
      const { ex, memberId, draft } = this.override;
      await this.submitOverride(ex, {
        athlete: memberId,
        swap: draft.swap.trim(),
        load_pct: parsed.value,
        sets: draft.sets.trim(),
        reps: draft.reps.trim(),
        note: draft.note.trim(),
      });
    },

    async clearOverride() {
      if (!this.override || this.override.saving) return;
      const { ex, memberId } = this.override;
      await this.submitOverride(ex, { athlete: memberId, clear: true });
    },

    // POST the override change, repaint the row's adj badge from the reply, and
    // close the editor. On failure the editor stays open with an error so the
    // coach can retry without losing their edits.
    async submitOverride(ex, body) {
      this.override.saving = true;
      this.override.error = "";
      try {
        const data = await this.apiPost(
          `/meso/api/plan/${this.planId}/prescription/${ex.id}/override/`,
          body,
        );
        ex.adj = data.adj || null;
        ex.adjusts = data.adjusts || [];
        this.override.saving = false; // clear before the guarded close
        this.closeOverride();
      } catch (err) {
        console.error("Override save failed", err);
        this.override.error = "Couldn't save that adjust. Please try again.";
        this.override.saving = false;
      }
    },

    // ---- coach 1RM editor (individual mode) ----
    //
    // On an individual plan a %1RM target ("75%") needs the athlete's max to
    // resolve to a bar load. The designer shows the athlete's stored 1RM (their
    // log-derived estimate or their own manual value) on a %1RM row; the coach
    // can set or override it here, persisted server-side as the athlete's own
    // (source=manual) number — the 1RM Phase 3 companion to the athlete logger's
    // input. Group rows have no single athlete, so the editor is individual-only.

    openOneRm(ex) {
      // Only an individual plan's %1RM row is editable here; a group plan's rows
      // belong to many athletes (use the per-athlete override editor instead).
      if (this.isGroup || !this.live || !ex || ex.load_type !== "pct") return;
      this.oneRm = { ex, value: ex.one_rm || "", saving: false, error: "" };
    },

    closeOneRm() {
      // Don't dismiss mid-save (mirrors closeOverride): a failed save keeps the
      // editor open to surface a retry, which needs this.oneRm to stay non-null.
      if (this.oneRm && this.oneRm.saving) return;
      this.oneRm = null;
    },

    // Parse the input to the value the endpoint expects: "" clears (back to the
    // log-derived estimate), a positive number sets, anything else is rejected
    // here so the badge never repaints off a server 400.
    parseOneRm() {
      const raw = (this.oneRm.value || "").trim();
      if (raw === "") return { ok: true, value: "" };
      if (!this.numeric(raw) || parseFloat(raw) <= 0) return { ok: false };
      return { ok: true, value: raw };
    },

    async saveOneRm() {
      if (!this.oneRm || this.oneRm.saving) return;
      const parsed = this.parseOneRm();
      if (!parsed.ok) {
        this.oneRm.error = "Enter a positive number, or leave blank to clear.";
        return;
      }
      const { ex } = this.oneRm;
      this.oneRm.saving = true;
      this.oneRm.error = "";
      try {
        const data = await this.apiPost(
          `/meso/api/plan/${this.planId}/prescription/${ex.id}/one-rm/`,
          { value: parsed.value },
        );
        ex.one_rm = data.one_rm || "";
        ex.one_rm_source = data.source || "";
        this.oneRm.saving = false; // clear before the guarded close
        this.closeOneRm();
      } catch (err) {
        console.error("1RM save failed", err);
        this.oneRm.error = "Couldn't save that 1RM. Please try again.";
        this.oneRm.saving = false;
      }
    },

    // ---- first-run coachmarks (Phase 5) ----
    //
    // The localStorage key for one region note's dismissal — namespaced under
    // `-designer-` so it never collides with the athlete onboarding coachmarks
    // (meso_onboarding.js uses the `meso-coachmark-` prefix too).
    coachmarkStorageKey(key) {
      return "meso-coachmark-designer-" + key;
    },
    // Read persisted dismissals into reactive state. Storage can be absent/throw
    // (Safari private mode) — treat that as "nothing dismissed".
    loadCoachmarks() {
      const next = {};
      for (let i = 0; i < this.coachmarkKeys.length; i++) {
        const key = this.coachmarkKeys[i];
        if (this.readCoachmark(key)) next[key] = true;
      }
      this.coachmarksDismissed = next;
    },
    readCoachmark(key) {
      try {
        const store = typeof window !== "undefined" && window.localStorage;
        return !!store && store.getItem(this.coachmarkStorageKey(key)) === "1";
      } catch (e) {
        return false;
      }
    },
    coachmarkVisible(key) {
      return !this.coachmarksDismissed[key];
    },
    // Hide a note and remember it. Reassign the map (not mutate) so Alpine
    // re-evaluates `x-show`; persist best-effort (the note hides regardless).
    dismissCoachmark(key) {
      this.coachmarksDismissed = { ...this.coachmarksDismissed, [key]: true };
      try {
        const store = typeof window !== "undefined" && window.localStorage;
        if (store) store.setItem(this.coachmarkStorageKey(key), "1");
      } catch (e) {
        /* best-effort — hidden in-page via reactive state regardless */
      }
    },

    // ---- helpers ----
    numeric(v) {
      const s = String(v == null ? "" : v).trim();
      return s !== "" && /^[0-9.]+$/.test(s);
    },
    round25(n) {
      return Math.round(n / 2.5) * 2.5;
    },
    barH(pct, track) {
      return Math.max(6, (pct / 100) * track) + "px";
    },
    // The Load cell's suffix: "%" for a %1RM row, the plan's unit for an
    // absolute (or typeless) one, nothing for a non-numeric load ("BW").
    loadSuffix(ex) {
      if (!this.numeric(ex && ex.load)) return "";
      return ex.load_type === "pct" ? "%" : this.unit;
    },
    // Flip a row between an absolute load and a % of 1RM, then autosave it.
    toggleLoadType(ex) {
      ex.load_type = ex.load_type === "pct" ? "abs" : "pct";
      return this.persistRow(ex);
    },

    // ---- periodization "calendar" chart cell helpers ----
    cellOn(w, ci) {
      return this.sessionDays.indexOf(ci) >= 0 && !(w.deload && ci === 4);
    },
    cellStyle(w, ci) {
      const on = this.cellOn(w, ci);
      const border = w.current ? "var(--soft-line)" : "var(--line)";
      const bg = on
        ? w.current
          ? "var(--accent)"
          : "var(--soft)"
        : "var(--rail)";
      return (
        "height:34px;border-radius:7px;border:1px solid " +
        border +
        ";background:" +
        bg +
        ";display:flex;align-items:center;justify-content:center"
      );
    },

    // ---- athlete (phone) view: first day, first three lifts ----
    get athleteDay() {
      const day = this.program[0];
      if (!day) return []; // a plan whose current week has no sessions yet
      return day.exercises.slice(0, 3).map((x, xi) => {
        const setN = parseInt(x.sets, 10) || 3;
        const rows = [];
        for (let i = 0; i < setN; i++) {
          const k = "a0-" + xi + "-" + i;
          const target =
            x.reps +
            " × " +
            (this.numeric(x.load)
              ? x.load + (x.load_type === "pct" ? "%" : " " + this.unit)
              : x.load);
          rows.push({ k, n: i + 1, target, done: !!this.checks[k] });
        }
        return { id: x.id, name: x.name, target: x.sets + "×" + x.reps, rows };
      });
    },
    get aTotal() {
      return this.athleteDay.reduce((acc, e) => acc + e.rows.length, 0);
    },
    get aDone() {
      return this.athleteDay.reduce(
        (acc, e) => acc + e.rows.filter((r) => r.done).length,
        0,
      );
    },

    // ---- navigation ----
    scrollThread() {
      this.$nextTick(() => {
        const t = this.$refs.thread;
        if (t) t.scrollTop = t.scrollHeight;
      });
    },

    toggleCheck(k) {
      this.checks[k] = !this.checks[k];
    },

    onDeliver() {
      this.delivered = true;
      setTimeout(() => {
        this.delivered = false;
      }, 2800);
    },

    async addExercise(di) {
      const day = this.program[di];
      if (this.live) {
        try {
          const data = await this.apiPost(
            `/meso/api/plan/${this.planId}/session/${day.id}/exercise/`,
            null,
          );
          day.exercises.push(data.prescription);
        } catch (err) {
          console.error("Add exercise failed", err);
        }
        return;
      }
      day.exercises.push({
        id: "n" + this.exSeq++,
        name: "New exercise",
        sets: "3",
        reps: "10",
        load: "",
        load_type: "abs",
        rpe: "7",
        note: "",
      });
    },

    // Add a training day to the *viewed* week (first-time-UX Phase 1; week-scoped
    // for the multi-week switcher). The server appends the Session to that week and
    // returns it in the grid's day shape; we push it so the new
    // (empty-but-for-a-starter-row) day appears without a reload.
    async addDay() {
      if (this.live) {
        try {
          const data = await this.apiPost(
            `/meso/api/plan/${this.planId}/session/`,
            { week_id: this.viewedWeekId },
          );
          this.program.push(data.session);
        } catch (err) {
          console.error("Add day failed", err);
        }
        return;
      }
      const n = this.program.length + 1;
      this.program.push({
        id: "d" + this.exSeq++,
        n,
        name: "Day " + n,
        bias: "",
        exercises: [],
      });
    },

    // ---- multi-week designer (view / add / set-current) ----
    //
    // The plan is multi-week: the strip switches which week the grid shows, a
    // coach can append the next week (the server copies the latest week's grid),
    // and any week can be made the live (deliver-target) one. Each action hits a
    // re-serialize endpoint and swaps the grid via applyPlanData.

    // Swap the grid to a (re)serialized plan payload. Shared by init, switchWeek,
    // addWeek, setCurrentWeek so the program / week strip / phases / viewed-week
    // pointer always move together.
    applyPlanData(data) {
      this.program = data.program;
      this.weeks = data.weeks;
      this.phases = data.phases;
      this.viewedWeekId = data.viewing != null ? data.viewing : null;
    },

    // Switch the grid to another week — a pure read (viewing never changes what's
    // live). No-op when already viewing it or before a plan is loaded.
    async switchWeek(weekId) {
      if (!this.live || weekId == null || weekId === this.viewedWeekId) return;
      try {
        const res = await fetch(`/meso/api/plan/${this.planId}/week/${weekId}/`);
        if (!res.ok) throw new Error("Request failed: " + res.status);
        this.applyPlanData(await res.json());
      } catch (err) {
        console.error("Switch week failed", err);
      }
    },

    // Append the next week to the active block and switch onto it. The server
    // copies the latest week's grid so the new week starts from a real template.
    async addWeek() {
      if (!this.live) return;
      try {
        const data = await this.apiPost(`/meso/api/plan/${this.planId}/week/`, null);
        this.applyPlanData(data);
      } catch (err) {
        console.error("Add week failed", err);
      }
    },

    // Make a week the live (deliver-target) week — delivery sends current_week.
    async setCurrentWeek(weekId) {
      if (!this.live || weekId == null) return;
      try {
        const data = await this.apiPost(
          `/meso/api/plan/${this.planId}/week/${weekId}/current/`,
          null,
        );
        this.applyPlanData(data);
      } catch (err) {
        console.error("Set current week failed", err);
      }
    },

    // ---- agent chat ----
    //
    // Each coach turn POSTs to the real proposal endpoint and renders the
    // returned batch inline. The agent only proposes — the program grid is not
    // mutated here; the coach applies (or discards) the batch at the review gate.
    pushCoach(text) {
      this.messages.push({ id: Date.now(), role: "coach", text });
      this.scrollThread();
    },
    pushAgent(msg) {
      this.messages.push({ id: Date.now() + 1, role: "agent", ...msg });
      this.scrollThread();
    },
    onInputKey(e) {
      if (e.key === "Enter") {
        e.preventDefault();
        this.onSend();
      }
    },
    onSend() {
      const t = (this.inputText || "").trim();
      if (!t || this.agentTyping) return;
      this.inputText = "";
      this.send(t);
    },
    onChip(label) {
      if (this.agentTyping) return;
      this.send(label);
    },

    send(instruction) {
      this.pushCoach(instruction);
      this.sendInstruction(instruction);
    },

    // POST the instruction to kick off the background job, then poll the batch's
    // status until it lands and render it (or an error).
    async sendInstruction(instruction) {
      if (!this.live) {
        this.pushAgent({
          text: "Load an athlete's plan first — there's nothing for me to edit yet.",
          error: true,
        });
        return;
      }
      this.agentTyping = true;
      this.scrollThread();
      try {
        const res = await fetch(`/meso/api/plan/${this.planId}/agent/`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": this.csrf,
          },
          body: JSON.stringify({ instruction }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          this.pushAgent({ text: this.agentErrorText(res.status, data), error: true });
          return;
        }
        await this.pollBatch(data.status_url);
      } catch (err) {
        console.error("Agent request failed", err);
        this.pushAgent({
          text: "Something went wrong reaching the agent. Please try again.",
          error: true,
        });
      } finally {
        this.agentTyping = false;
        this.scrollThread();
      }
    },

    // Poll the batch's status endpoint while the background job runs. Resolves
    // (rendering the batch or an error) when the batch lands, fails, or the poll
    // gives up — the caller clears `agentTyping` afterward.
    async pollBatch(statusUrl) {
      if (!statusUrl) {
        this.pushAgent({
          text: "The agent couldn't process that request.",
          error: true,
        });
        return;
      }
      for (let attempt = 0; attempt < this.pollMaxAttempts; attempt++) {
        let data;
        try {
          const res = await fetch(statusUrl);
          data = await res.json().catch(() => ({}));
          if (!res.ok) {
            this.pushAgent({
              text: this.agentErrorText(res.status, data),
              error: true,
            });
            return;
          }
        } catch (err) {
          console.error("Agent status poll failed", err);
          this.pushAgent({
            text: "Something went wrong reaching the agent. Please try again.",
            error: true,
          });
          return;
        }
        if (data.status === "drafting") {
          await this.sleep(this.pollIntervalMs);
          continue;
        }
        if (data.status === "failed") {
          this.pushAgent({
            text: data.error || "The agent had trouble responding. Give it another try.",
            error: true,
          });
          return;
        }
        // pending / applied / dismissed — a resolved batch.
        this.pushAgent(this.batchMessage(data));
        return;
      }
      this.pushAgent({
        text: "The agent is taking longer than expected. Check the review screen in a moment.",
        error: true,
      });
    },

    sleep(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    },

    // Shape the endpoint's batch response into an agent chat message. Changes
    // are inert here; the review link is the only way to act on them.
    batchMessage(data) {
      const changes = data.changes || [];
      let text = data.summary || "";
      if (!changes.length) {
        text =
          text ||
          "I couldn't find any safe changes to propose for that. Try rephrasing or adjusting the plan directly.";
      }
      return {
        text,
        changes,
        reviewUrl: changes.length ? data.review_url : null,
      };
    },

    agentErrorText(status, data) {
      if (status === 503)
        return "The agent isn't configured in this environment yet.";
      if (status === 502)
        return "The agent had trouble responding. Give it another try.";
      if (status === 400)
        return "That message couldn't be sent — try a shorter instruction.";
      return (data && data.error) || "The agent couldn't process that request.";
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
    Alpine.data("meso", () => createMeso());
  });
}

// Test hook: expose the factory to Node-based runners (vitest). Skipped in the
// browser, where `module` is undefined.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { createMeso };
}
