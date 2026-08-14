// Tests for the interval timer's timeline engine
// (app/store_project/static/js/timer.js).
//
// Focus: the pure functions the ticking clock delegates to -- how a form's
// settings compile into segments, where a given second lands, and which audio
// cue that second owes. Everything that could silently go wrong (a dropped
// trailing rest, a missing "go" at the top of an EMOM cycle, an off-by-one on
// the 3-2-1 countdown) lives there. The last suite drives the DOM controller
// over a fake clock to cover the wiring the pure functions can't see: which
// fields each timer type shows, and what the face reads while it runs.

import { readFileSync } from "node:fs";
import {
  initTimer,
  buildTimeline,
  segmentAt,
  stateAt,
  cueAt,
  getMinutes,
  getSeconds,
} from "../app/store_project/static/js/timer.js";

// Walk a whole workout and collect the cue owed on each second, 1..total.
// Second 0 is the handoff from the prep countdown, which plays its own "go".
function cueSequence(timeline) {
  const cues = [];
  for (let second = 1; second <= timeline.totalDuration; second++) {
    cues.push(cueAt(timeline, second));
  }
  return cues;
}

function workRest({ rounds, workSeconds, restSeconds }) {
  return buildTimeline({
    mode: "work-rest",
    rounds,
    workSeconds,
    restSeconds,
    intervalSeconds: 60,
  });
}

function emom({ rounds, intervalSeconds }) {
  return buildTimeline({
    mode: "emom",
    rounds,
    workSeconds: 30,
    restSeconds: 30,
    intervalSeconds,
  });
}

describe("getMinutes / getSeconds", () => {
  it("pads to two digits", () => {
    expect(getMinutes(0)).toBe("00");
    expect(getSeconds(0)).toBe("00");
    expect(getMinutes(75)).toBe("01");
    expect(getSeconds(75)).toBe("15");
  });
});

describe("buildTimeline: work / rest", () => {
  it("alternates work and rest and drops the trailing rest", () => {
    const timeline = workRest({ rounds: 3, workSeconds: 20, restSeconds: 10 });
    expect(
      timeline.segments.map((s) => [s.round, s.kind, s.seconds, s.start, s.end])
    ).toEqual([
      [1, "work", 20, 0, 20],
      [1, "rest", 10, 20, 30],
      [2, "work", 20, 30, 50],
      [2, "rest", 10, 50, 60],
      [3, "work", 20, 60, 80],
    ]);
    expect(timeline.totalDuration).toBe(80);
  });

  it("is a single work segment for one round", () => {
    const timeline = workRest({ rounds: 1, workSeconds: 45, restSeconds: 15 });
    expect(timeline.segments).toHaveLength(1);
    expect(timeline.totalDuration).toBe(45);
  });

  it("collapses a zero rest into a straight repeat", () => {
    const timeline = workRest({ rounds: 3, workSeconds: 20, restSeconds: 0 });
    expect(timeline.segments.map((s) => s.kind)).toEqual([
      "work",
      "work",
      "work",
    ]);
    expect(timeline.totalDuration).toBe(60);
  });

  it("is empty when the form has no usable numbers", () => {
    const timeline = workRest({ rounds: 0, workSeconds: 0, restSeconds: 0 });
    expect(timeline.segments).toEqual([]);
    expect(timeline.totalDuration).toBe(0);
  });
});

describe("buildTimeline: EMOM", () => {
  it("is one work segment per round, with no rest", () => {
    const timeline = emom({ rounds: 3, intervalSeconds: 60 });
    expect(
      timeline.segments.map((s) => [s.round, s.kind, s.seconds, s.start, s.end])
    ).toEqual([
      [1, "work", 60, 0, 60],
      [2, "work", 60, 60, 120],
      [3, "work", 60, 120, 180],
    ]);
    expect(timeline.totalDuration).toBe(180);
  });

  it("is not tied to the minute -- the cycle is whatever you set", () => {
    // E2MOM and E90s, the cases issue #494 asks for.
    expect(emom({ rounds: 5, intervalSeconds: 120 }).totalDuration).toBe(600);
    expect(
      emom({ rounds: 4, intervalSeconds: 90 }).segments.map((s) => s.start)
    ).toEqual([0, 90, 180, 270]);
  });

  it("ignores the work and rest fields entirely", () => {
    const timeline = buildTimeline({
      mode: "emom",
      rounds: 2,
      workSeconds: 999,
      restSeconds: 999,
      intervalSeconds: 60,
    });
    expect(timeline.totalDuration).toBe(120);
  });
});

describe("segmentAt / stateAt", () => {
  const timeline = workRest({ rounds: 2, workSeconds: 5, restSeconds: 4 });
  // work 1 [0,5)  rest 1 [5,9)  work 2 [9,14)

  it("hands the boundary second to the segment starting on it", () => {
    expect(segmentAt(timeline, 4).kind).toBe("work");
    expect(segmentAt(timeline, 5).kind).toBe("rest");
    expect(segmentAt(timeline, 8).kind).toBe("rest");
    expect(segmentAt(timeline, 9).kind).toBe("work");
  });

  it("reports the round, the phase, and the time left in it", () => {
    expect(stateAt(timeline, 0)).toEqual({
      round: 1,
      kind: "work",
      secondsLeft: 5,
      finished: false,
    });
    expect(stateAt(timeline, 6)).toEqual({
      round: 1,
      kind: "rest",
      secondsLeft: 3,
      finished: false,
    });
    expect(stateAt(timeline, 9)).toEqual({
      round: 2,
      kind: "work",
      secondsLeft: 5,
      finished: false,
    });
  });

  it("finishes on the last segment's final second, holding the last round", () => {
    expect(stateAt(timeline, 13).finished).toBe(false);
    expect(stateAt(timeline, 14)).toEqual({
      round: 2,
      kind: "finished",
      secondsLeft: 0,
      finished: true,
    });
    expect(stateAt(timeline, 99).finished).toBe(true);
  });

  it("counts EMOM rounds down within the cycle", () => {
    const cycles = emom({ rounds: 3, intervalSeconds: 60 });
    expect(stateAt(cycles, 0)).toMatchObject({ round: 1, secondsLeft: 60 });
    expect(stateAt(cycles, 59)).toMatchObject({ round: 1, secondsLeft: 1 });
    expect(stateAt(cycles, 60)).toMatchObject({ round: 2, secondsLeft: 60 });
    expect(stateAt(cycles, 180).finished).toBe(true);
  });

  it("reports finished for an empty timeline rather than throwing", () => {
    const empty = workRest({ rounds: 0, workSeconds: 0, restSeconds: 0 });
    expect(segmentAt(empty, 0)).toBeNull();
    expect(stateAt(empty, 0)).toEqual({
      round: 1,
      kind: "finished",
      secondsLeft: 0,
      finished: true,
    });
  });
});

describe("cueAt: work / rest", () => {
  // Locks in the cue schedule the timer shipped with, second for second, so
  // the timeline rewrite can't quietly change how an existing workout sounds.
  it("counts into every phase change and ends on the rest bell", () => {
    const timeline = workRest({ rounds: 2, workSeconds: 5, restSeconds: 4 });
    expect(cueSequence(timeline)).toEqual([
      null, // 1  work 1
      "three", // 2
      "two", // 3
      "one", // 4
      "rest", // 5  work 1 done
      "three", // 6
      "two", // 7
      "one", // 8
      "go", // 9  round 2 starts
      null, // 10
      "three", // 11
      "two", // 12
      "one", // 13
      "rest", // 14 workout over
    ]);
  });

  it("says nothing on second 0 -- the prep countdown owns the first go", () => {
    const timeline = workRest({ rounds: 2, workSeconds: 5, restSeconds: 4 });
    expect(cueAt(timeline, 0)).toBeNull();
  });
});

describe("cueAt: EMOM", () => {
  it("fires go at the top of every cycle and never calls a rest", () => {
    const timeline = emom({ rounds: 3, intervalSeconds: 5 });
    expect(cueSequence(timeline)).toEqual([
      null, // 1  cycle 1
      "three", // 2
      "two", // 3
      "one", // 4
      "go", // 5  cycle 2 -- on the interval, no rest phase
      null, // 6
      "three", // 7
      "two", // 8
      "one", // 9
      "go", // 10 cycle 3
      null, // 11
      "three", // 12
      "two", // 13
      "one", // 14
      "rest", // 15 workout over
    ]);
  });

  it("puts a go on every interval boundary of a long EMOM", () => {
    const timeline = emom({ rounds: 10, intervalSeconds: 60 });
    const gos = [];
    for (let second = 1; second < timeline.totalDuration; second++) {
      if (cueAt(timeline, second) === "go") gos.push(second);
    }
    expect(gos).toEqual([60, 120, 180, 240, 300, 360, 420, 480, 540]);
  });

  it("counts down into each interval, not out of a rest", () => {
    const timeline = emom({ rounds: 2, intervalSeconds: 90 });
    expect(cueAt(timeline, 87)).toBe("three");
    expect(cueAt(timeline, 88)).toBe("two");
    expect(cueAt(timeline, 89)).toBe("one");
    expect(cueAt(timeline, 90)).toBe("go");
    expect(cueAt(timeline, 91)).toBeNull();
  });
});

/*
 * The DOM controller, over a fake clock. Mirrors pages/timer.html.
 */

const CUE_NAMES = ["one", "two", "three", "rest", "go"];

// The real stylesheet, mounted with the markup. Hiding a field is a cascade
// question -- `.field { display: flex }` is declared after `.hidden` at the
// same specificity -- so a class check alone proves nothing. Read from disk
// rather than imported: vitest stubs `.css` imports out to an empty module.
// (Path is relative to the repo root, where vitest runs.)
const TIMER_CSS = readFileSync(
  "app/store_project/static/css/timer.css",
  "utf8"
);

// The audio bank the template declares ahead of timer.js, stubbed so the tests
// can read back the cue order the controller actually played.
function stubAudio() {
  const played = [];
  globalThis.audio = Object.fromEntries(
    CUE_NAMES.map((name) => [name, { play: () => played.push(name) }])
  );
  return played;
}

function mountTimer() {
  const style = document.createElement("style");
  style.textContent = TIMER_CSS;
  document.head.replaceChildren(style);

  document.body.innerHTML = `
    <main class="content">
      <div id="timer-display">
        <div id="countdown"><span id="minutes"></span><span id="seconds"></span></div>
        <div class="bar-container">
          <div id="progress-overlay"></div>
          <div class="progress" id="progress-container"></div>
        </div>
        <div id="cycles"><span id="current-round"></span><span id="total-rounds"></span></div>
      </div>
      <form class="stack" id="timer-form">
        <div class="field">
          <select id="mode" name="mode">
            <option value="work-rest">Work / Rest</option>
            <option value="emom">Repeating (EMOM)</option>
          </select>
        </div>
        <div class="field">
          <input type="number" id="rounds" name="rounds" min="1" value="30" required />
        </div>
        <div class="field" data-mode="work-rest">
          <input type="number" id="work" name="work" min="1" value="30" required />
        </div>
        <div class="field" data-mode="work-rest">
          <input type="number" id="rest" name="rest" min="1" value="30" required />
        </div>
        <div class="field hidden" data-mode="emom">
          <input type="number" id="interval" name="interval" min="1" value="60" />
        </div>
        <div class="field">
          <input type="number" id="prep" name="prep" min="1" value="10" required />
        </div>
        <div class="buttons">
          <button class="button start" id="start" type="submit">Start</button>
          <button class="button outline reset" id="reset">Reset</button>
        </div>
      </form>
    </main>
  `;
  initTimer();

  const $ = (selector) => document.querySelector(selector);
  return {
    content: $(".content"),
    face: () => `${$("#minutes").innerHTML}:${$("#seconds").innerHTML}`,
    round: () => $("#current-round").innerHTML,
    totalRounds: () => $("#total-rounds").innerHTML,
    bars: () =>
      Array.from(document.querySelectorAll("#progress-container .progress-bar")),
    fieldFor: (id) => $(`#${id}`).closest(".field"),
    shows: (id) =>
      window.getComputedStyle($(`#${id}`).closest(".field")).display !== "none",
    set(id, value) {
      const input = $(`#${id}`);
      input.value = String(value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    },
    chooseMode(value) {
      $("#mode").value = value;
      $("#mode").dispatchEvent(new Event("change", { bubbles: true }));
    },
    start: () =>
      $("#timer-form").dispatchEvent(new Event("submit", { cancelable: true })),
    // A real click, so native constraint validation gets its say.
    press: () => $("#start").click(),
    startLabel: () => $("#start").innerHTML,
    reset: () => $("#reset").dispatchEvent(new Event("click", { bubbles: true })),
  };
}

describe("initTimer: choosing a timer type", () => {
  beforeEach(() => {
    stubAudio();
  });

  it("swaps work/rest for the repeat interval, and back", () => {
    const ui = mountTimer();
    // Computed display, not the class: `.field { display: flex }` is declared
    // after `.hidden` at the same specificity, so a class check would pass
    // while both sets of inputs stayed on screen.
    expect(ui.shows("work")).toBe(true);
    expect(ui.shows("interval")).toBe(false);

    ui.chooseMode("emom");
    expect(ui.shows("work")).toBe(false);
    expect(ui.shows("rest")).toBe(false);
    expect(ui.shows("interval")).toBe(true);

    ui.chooseMode("work-rest");
    expect(ui.shows("work")).toBe(true);
    expect(ui.shows("rest")).toBe(true);
    expect(ui.shows("interval")).toBe(false);

    // Rounds and prep belong to both types.
    expect(ui.shows("rounds")).toBe(true);
    expect(ui.shows("prep")).toBe(true);
  });

  it("disables the hidden fields so they can't block submit", () => {
    const ui = mountTimer();
    expect(document.querySelector("#interval").required).toBe(false);
    expect(document.querySelector("#interval").disabled).toBe(true);

    ui.chooseMode("emom");
    expect(document.querySelector("#work").required).toBe(false);
    expect(document.querySelector("#work").disabled).toBe(true);
    expect(document.querySelector("#rest").disabled).toBe(true);
    expect(document.querySelector("#interval").required).toBe(true);
    expect(document.querySelector("#interval").disabled).toBe(false);
  });

  it("keeps the form submittable with an out-of-range value hidden", () => {
    // A disabled control is barred from validation; an un-required one is not,
    // so `min="1"` on an off-screen 0 would block Start with nothing to click.
    const ui = mountTimer();
    const valid = () => document.querySelector("#timer-form").checkValidity();

    ui.set("work", 0); // visible: the min really does bite
    expect(valid()).toBe(false);
    ui.set("work", 30);
    expect(valid()).toBe(true);

    ui.set("interval", 0); // hidden: same value, no longer in the way
    expect(valid()).toBe(true);

    ui.chooseMode("emom"); // now work is the hidden one
    ui.set("interval", 60);
    ui.set("work", 0);
    expect(valid()).toBe(true);
  });

  it("previews the cycle length and one bar per round", () => {
    const ui = mountTimer();
    expect(ui.face()).toBe("00:30"); // the work duration

    ui.chooseMode("emom");
    ui.set("rounds", 4);
    ui.set("interval", 90);

    expect(ui.face()).toBe("01:30");
    expect(ui.totalRounds()).toBe("4");
    const bars = ui.bars();
    expect(bars).toHaveLength(4); // no rest bars in between
    expect(bars.map((bar) => bar.textContent)).toEqual(["1", "2", "3", "4"]);
    expect(bars.every((bar) => bar.style.width === "25%")).toBe(true);
  });
});

describe("initTimer: running an EMOM", () => {
  let played;

  beforeEach(() => {
    vi.useFakeTimers();
    played = stubAudio();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function startEmom({ rounds, interval, prep }) {
    const ui = mountTimer();
    ui.chooseMode("emom");
    ui.set("rounds", rounds);
    ui.set("interval", interval);
    ui.set("prep", prep);
    ui.start();
    vi.advanceTimersByTime(prep * 1000); // run out the prep countdown
    return ui;
  }

  it("counts each cycle down and calls go on the interval", () => {
    const ui = startEmom({ rounds: 2, interval: 4, prep: 1 });
    expect(ui.face()).toBe("00:04");
    expect(ui.round()).toBe("1");
    expect(played).toEqual(["go"]); // end of prep

    vi.advanceTimersByTime(3000);
    expect(ui.face()).toBe("00:01");
    expect(played).toEqual(["go", "three", "two", "one"]);

    vi.advanceTimersByTime(1000); // top of cycle 2
    expect(ui.face()).toBe("00:04");
    expect(ui.round()).toBe("2");
    expect(played.at(-1)).toBe("go");
    expect(ui.content.classList.contains("working")).toBe(true);
    expect(ui.content.classList.contains("resting")).toBe(false);
  });

  it("never drops into a rest phase, and finishes on the last cycle", () => {
    const ui = startEmom({ rounds: 2, interval: 4, prep: 1 });

    vi.advanceTimersByTime(8000);
    expect(ui.face()).toBe("00:00");
    expect(ui.content.classList.contains("finished")).toBe(true);
    expect(ui.content.classList.contains("working")).toBe(false);
    // "rest" only ever rings once: the finish bell.
    expect(played.filter((cue) => cue === "rest")).toHaveLength(1);
    expect(played.at(-1)).toBe("rest");

    // The clock is stopped -- nothing keeps ticking past the end.
    const face = ui.face();
    vi.advanceTimersByTime(5000);
    expect(ui.face()).toBe(face);
    expect(ui.startLabel()).toBe("Start");
  });

  it("ignores form edits while the clock is running", () => {
    const ui = startEmom({ rounds: 2, interval: 4, prep: 1 });
    vi.advanceTimersByTime(1000);
    expect(ui.face()).toBe("00:03");

    ui.set("interval", 60); // second thoughts, mid-cycle

    expect(ui.face()).toBe("00:03"); // still on the cycle it started
    vi.advanceTimersByTime(3000);
    expect(ui.round()).toBe("2");
    expect(ui.face()).toBe("00:04");
  });

  it("pauses and resumes on real clicks even with a field mid-edit", () => {
    // Pause and Resume share the submit button with Start, so a half-typed
    // interval would fail validation, swallow the submit, and leave the
    // button dead -- pointing at a field that isn't even read on those paths.
    const ui = startEmom({ rounds: 3, interval: 4, prep: 1 });
    vi.advanceTimersByTime(1000);
    ui.set("interval", ""); // second thoughts about the next run

    ui.press();

    expect(ui.startLabel()).toBe("Resume");
    const stopped = ui.face();
    vi.advanceTimersByTime(3000);
    expect(ui.face()).toBe(stopped);

    ui.press();

    expect(ui.startLabel()).toBe("Pause");
    vi.advanceTimersByTime(1000);
    expect(ui.face()).not.toBe(stopped);
  });

  it("ignores form edits while paused, and resumes where it left off", () => {
    const ui = startEmom({ rounds: 3, interval: 4, prep: 1 });
    vi.advanceTimersByTime(2000); // two seconds into cycle 1
    expect(ui.face()).toBe("00:02");

    ui.start(); // the Start button is a Pause button now
    expect(ui.startLabel()).toBe("Resume");

    ui.set("interval", 60); // a new timeline would strand elapsedSeconds
    ui.chooseMode("work-rest");
    expect(ui.face()).toBe("00:02");

    ui.start();
    vi.advanceTimersByTime(2000); // cycle 1 runs out on its original length
    expect(ui.round()).toBe("2");
    expect(ui.face()).toBe("00:04");
  });

  it("resets back to the idle preview", () => {
    const ui = startEmom({ rounds: 2, interval: 4, prep: 1 });
    vi.advanceTimersByTime(2000);

    ui.reset();
    expect(ui.face()).toBe("00:04");
    expect(ui.round()).toBe("1");
    expect(ui.startLabel()).toBe("Start");
    expect(ui.content.className).toBe("content");
  });
});

describe("initTimer: running work / rest", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    stubAudio();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("still counts total elapsed time and swaps the phase colour", () => {
    const ui = mountTimer();
    ui.set("rounds", 2);
    ui.set("work", 3);
    ui.set("rest", 2);
    ui.set("prep", 1);
    ui.start();
    vi.advanceTimersByTime(1000); // prep done

    expect(ui.face()).toBe("00:00");
    expect(ui.content.classList.contains("working")).toBe(true);

    vi.advanceTimersByTime(3000); // work 1 done -> resting
    expect(ui.face()).toBe("00:03");
    expect(ui.content.classList.contains("resting")).toBe(true);

    vi.advanceTimersByTime(2000); // round 2 begins
    expect(ui.face()).toBe("00:05");
    expect(ui.round()).toBe("2");
    expect(ui.content.classList.contains("working")).toBe(true);
  });

  it("stays put when there is nothing to run", () => {
    const ui = mountTimer();
    ui.set("rounds", "");
    ui.start();

    expect(ui.startLabel()).toBe("Start");
    vi.advanceTimersByTime(60000);
    expect(ui.content.classList.contains("preparing")).toBe(false);
    expect(ui.content.classList.contains("working")).toBe(false);
  });
});
