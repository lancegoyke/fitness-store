/* Meso — AI strength-training program designer.
 *
 * A faithful port of the Meso.dc.html Claude Design prototype to Alpine.js.
 * All program/agent state is client-side; the "agent" is a canned intent
 * engine (swap-knee / lower-volume / progress / deload) that mutates the
 * in-memory program, exactly as the original prototype did. Replacing
 * dispatch()/applyIntent() with a real backend call is the seam to make this
 * live.
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

    messages: [
      {
        id: 1,
        role: "agent",
        text: "Here's Week 2 of Maya's hypertrophy block — 3 sessions, all knee-safe. I used box squats to parallel instead of back squats and kept deep knee flexion out of the loaded work.",
        change: {
          title: "Drafted Week 2 · 3 sessions",
          detail: "Honors: avoid deep knee flexion under load",
        },
      },
      {
        id: 2,
        role: "coach",
        text: "Nice. Bump her trap-bar pull — she sat at RPE 6 last block.",
      },
      {
        id: 3,
        role: "agent",
        text: "Done. Progressed the trap-bar deadlift to 92.5 kg. She logged 4×6 @ 90 / RPE 6 last session, so this lands around RPE 7 — right in the hypertrophy window.",
        change: {
          title: "Trap-Bar Deadlift → 92.5 kg",
          detail: "From logged 4×6 @ 90 kg · RPE 6",
        },
      },
    ],

    program: [
      {
        id: "d1",
        n: 1,
        name: "Lower",
        bias: "Quad bias · knee-safe",
        exercises: [
          {
            id: "e1",
            name: "Box Squat (to parallel)",
            sets: "4",
            reps: "6",
            load: "70",
            rpe: "7",
            note: "",
            last: "4×6 · 70kg · RPE6",
            tag: "knee-safe",
            adj: "Maya → box",
          },
          {
            id: "e2",
            name: "Bulgarian Split Squat (DB)",
            sets: "3",
            reps: "10",
            load: "18",
            rpe: "7",
            note: "",
            last: "3×10 · 16kg",
          },
          {
            id: "e3",
            name: "Leg Press (controlled ROM)",
            sets: "3",
            reps: "12",
            load: "110",
            rpe: "8",
            note: "",
          },
          {
            id: "e4",
            name: "Seated Leg Curl",
            sets: "3",
            reps: "12",
            load: "41",
            rpe: "8",
            note: "",
          },
          {
            id: "e5",
            name: "Standing Calf Raise",
            sets: "4",
            reps: "15",
            load: "60",
            rpe: "—",
            note: "",
          },
        ],
      },
      {
        id: "d2",
        n: 2,
        name: "Upper",
        bias: "Push / pull",
        exercises: [
          {
            id: "e6",
            name: "Incline DB Press",
            sets: "4",
            reps: "8",
            load: "24",
            rpe: "7",
            note: "monitor shoulder",
            adj: "Devon → neutral grip",
          },
          {
            id: "e7",
            name: "Chest-Supported Row",
            sets: "4",
            reps: "10",
            load: "27",
            rpe: "7",
            note: "",
          },
          {
            id: "e8",
            name: "Lat Pulldown",
            sets: "3",
            reps: "12",
            load: "52",
            rpe: "8",
            note: "",
          },
          {
            id: "e9",
            name: "DB Shoulder Press",
            sets: "3",
            reps: "10",
            load: "16",
            rpe: "7",
            note: "neutral grip",
          },
          {
            id: "e10",
            name: "Cable Lateral Raise",
            sets: "3",
            reps: "15",
            load: "9",
            rpe: "—",
            note: "",
          },
        ],
      },
      {
        id: "d3",
        n: 3,
        name: "Posterior",
        bias: "Hinge",
        exercises: [
          {
            id: "e11",
            name: "Trap-Bar Deadlift",
            sets: "4",
            reps: "6",
            load: "92.5",
            rpe: "7",
            note: "",
            last: "4×6 · 90kg · RPE6",
            adj: "Lena → RDL",
          },
          {
            id: "e12",
            name: "Hip Thrust",
            sets: "3",
            reps: "10",
            load: "80",
            rpe: "8",
            note: "",
          },
          {
            id: "e13",
            name: "Romanian Deadlift (3-1-1)",
            sets: "3",
            reps: "8",
            load: "60",
            rpe: "7",
            note: "tempo eccentric",
          },
          {
            id: "e14",
            name: "Reverse Lunge (DB)",
            sets: "3",
            reps: "12",
            load: "14",
            rpe: "—",
            note: "knee-monitored",
            tag: "knee-safe",
          },
          {
            id: "e15",
            name: "Hanging Knee Raise",
            sets: "3",
            reps: "12",
            load: "BW",
            rpe: "—",
            note: "",
          },
        ],
      },
    ],

    weeks: [
      {
        label: "Wk 1",
        phase: "Accum",
        vol: 70,
        inten: 62,
        deload: false,
        current: false,
      },
      {
        label: "Wk 2",
        phase: "Accum",
        vol: 85,
        inten: 68,
        deload: false,
        current: true,
      },
      {
        label: "Wk 3",
        phase: "Accum",
        vol: 100,
        inten: 73,
        deload: false,
        current: false,
      },
      {
        label: "Wk 4",
        phase: "Deload",
        vol: 55,
        inten: 70,
        deload: true,
        current: false,
      },
    ],

    phases: [
      { name: "Base / GPP", weeks: "4 wk", state: "done" },
      { name: "Hypertrophy", weeks: "4 wk", state: "current" },
      { name: "Strength", weeks: "4 wk", state: "next" },
      { name: "Peak / Test", weeks: "2 wk", state: "future" },
    ],

    chips: [
      { label: "Lower Day 2 volume", intent: "lower-volume-d2" },
      { label: "Swap a knee-sensitive lift", intent: "swap-knee" },
      { label: "Progress from last block", intent: "progress" },
      { label: "Add a deload week", intent: "deload" },
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

    addExercise(di) {
      this.program[di].exercises.push({
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
    pushCoach(text) {
      this.messages.push({ id: Date.now(), role: "coach", text });
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
      if (!t) return;
      this.pushCoach(t);
      this.inputText = "";
      this.dispatch(this.detectIntent(t));
    },
    onChip(intent, label) {
      this.pushCoach(label);
      this.dispatch(intent);
    },

    detectIntent(t) {
      const s = t.toLowerCase();
      if (/knee|meniscus|swap|substitut|replace/.test(s)) return "swap-knee";
      if (/deload|recover|fatigue|back off/.test(s)) return "deload";
      if (/volume|lighter|reduce|less|trim|drop a set/.test(s))
        return "lower-volume-d2";
      if (/progress|heavier|bump|increase|overload|add (weight|load)/.test(s))
        return "progress";
      return "generic";
    },

    dispatch(intent) {
      this.agentTyping = true;
      this.scrollThread();
      setTimeout(() => this.applyIntent(intent), 780);
    },

    applyIntent(intent) {
      let msg;

      if (intent === "swap-knee") {
        const d = this.program[0];
        const ix = d.exercises.findIndex((x) => /bulgarian/i.test(x.name));
        const tgt = ix >= 0 ? ix : 1;
        Object.assign(d.exercises[tgt], {
          name: "Box Step-Down (low)",
          load: "14",
          tag: "knee-safe",
          note: "pain-free ROM, slow eccentric",
        });
        msg = {
          text: "Swapped the Bulgarian split squat for a low box step-down. Same single-leg quad stimulus, but the knee tracks through a shorter, controlled range — a better fit for the meniscus history.",
          change: {
            title: "Bulgarian Split Squat → Box Step-Down",
            detail: "Single-leg quad work · controlled ROM",
          },
        };
      } else if (intent === "lower-volume-d2") {
        const d = this.program[1];
        d.exercises.forEach((x, i) => {
          if (i < 3)
            x.sets = String(Math.max(2, (parseInt(x.sets, 10) || 3) - 1));
        });
        msg = {
          text: "Trimmed Day 2 — dropped a set on the three main upper-body lifts. Keeps weekly pressing volume in check while her shoulder settles, without touching the accessory work.",
          change: {
            title: "Day 2 volume − 1 set",
            detail: "Applied to the 3 primary lifts",
          },
        };
      } else if (intent === "progress") {
        this.program.forEach((d) => {
          d.exercises.forEach((x) => {
            const n = parseFloat(x.load);
            if (!isNaN(n) && this.numeric(x.load) && x.rpe !== "—") {
              x.load = String(this.round25(n + 2.5));
            }
          });
        });
        msg = {
          text: "Progressed the main lifts by ~2.5 kg across the week, anchored to last block's logged loads and RPEs. Accessories held steady so the added stimulus stays on the big patterns.",
          change: {
            title: "+2.5 kg on primary lifts",
            detail: "Driven by logged session data",
          },
        };
      } else if (intent === "deload") {
        this.view = "block";
        const w = this.weeks[3];
        Object.assign(w, { vol: 50, inten: 70, deload: true, phase: "Deload" });
        msg = {
          text: "Set Week 4 as a deload — volume drops ~45% while intensity holds near 70%. That clears accumulated fatigue and sets up a clean hand-off into the strength block.",
          change: {
            title: "Week 4 → Deload",
            detail: "Volume −45% · intensity held",
          },
        };
      } else {
        msg = {
          text: "Got it — I've noted that against Maya's profile and coaching rules. Want me to apply it to this week, or carry it into the next block's plan?",
        };
      }

      this.messages.push({
        id: Date.now() + 1,
        role: "agent",
        text: msg.text,
        change: msg.change,
      });
      this.agentTyping = false;
      this.scrollThread();
    },
  }));
});
