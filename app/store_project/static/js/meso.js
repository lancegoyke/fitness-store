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
document.addEventListener("alpine:init", () => {
  Alpine.data("meso", () => ({
    // ---- design tokens (the prototype exposed these as editor props) ----
    accent: "Cobalt",
    theme: "Clinical",
    unit: "kg",

    // ---- ui state ----
    mode: "individual", // individual | group
    view: "week", // week | block | athlete
    periodStyle: "timeline", // timeline | ladder | calendar
    inputText: "",
    agentTyping: false,
    delivered: false,
    checks: {},
    exSeq: 1,

    // ---- backend hydration (Phase 3) ----
    // init() flips `live` on and fills program/weeks/phases from the plan the
    // view injects. Without an injected plan nothing hydrates and no network
    // calls are made (the bare designer URL redirects to a real plan, Phase 5).
    live: false,
    planId: null,
    csrf: "",

    // The thread starts with a single orienting greeting. Real agent turns are
    // appended as the coach sends instructions; the thread is not persisted yet
    // (a later slice adds the background job + saved conversation).
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

    // ---- backend hydration + autosave (Phase 3) ----
    init() {
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
      this.program = data.program;
      this.weeks = data.weeks;
      this.phases = data.phases;
      const csrfEl = document.getElementById("meso-csrf");
      this.csrf = csrfEl ? csrfEl.dataset.token : "";
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
      this.apiPost(`/meso/api/plan/${this.planId}/prescription/${ex.id}/`, {
        name: ex.name ?? "",
        sets: ex.sets ?? "",
        reps: ex.reps ?? "",
        load: ex.load ?? "",
        rpe: ex.rpe ?? "",
        note: ex.note ?? "",
      }).catch((err) => console.error("Autosave failed", err));
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
    loadSuffix(load) {
      return this.numeric(load) ? this.unit : "";
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
            (this.numeric(x.load) ? x.load + " " + this.unit : x.load);
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
        rpe: "7",
        note: "",
      });
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

    // POST the instruction to the agent, then render the batch (or an error).
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
        this.pushAgent(this.batchMessage(data));
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
  }));
});
