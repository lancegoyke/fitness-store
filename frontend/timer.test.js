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
  reviseTimeline,
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
 * Editing a workout that is already under way (issue #496). The contract: what
 * has been played is history and never moves -- neither the rounds behind the
 * clock nor the one it is standing in -- and the new settings take over from
 * the next segment on. An edit can therefore never drop the athlete into a
 * different part of the workout, however hard the numbers change.
 */
describe("reviseTimeline", () => {
  const settings = {
    mode: "work-rest",
    rounds: 4,
    workSeconds: 20,
    restSeconds: 10,
    intervalSeconds: 60,
  };
  // w1 0-20, r1 20-30, w2 30-50, r2 50-60, w3 60-80, r3 80-90, w4 90-110
  const running = () => buildTimeline(settings);
  const shape = (timeline) =>
    timeline.segments.map((s) => [s.round, s.kind, s.seconds]);

  it("keeps the rounds already played, and the round in progress, intact", () => {
    const before = running();
    const position = 65; // five seconds into round 3's work

    const after = reviseTimeline(before, position, {
      ...settings,
      restSeconds: 5, // "shorten the rest by five" -- from here on
    });

    // Same second, same place in the workout: same round, same phase, same
    // time left on the face. This is the whole point of the exercise.
    expect(stateAt(after, position)).toEqual(stateAt(before, position));
    expect(after.segments.slice(0, 5)).toEqual(before.segments.slice(0, 5));
    expect(shape(after)).toEqual([
      [1, "work", 20],
      [1, "rest", 10], // played at the old cadence
      [2, "work", 20],
      [2, "rest", 10],
      [3, "work", 20], // the round in progress -- untouched
      [3, "rest", 5], // the new cadence starts here
      [4, "work", 20],
    ]);
    expect(after.totalDuration).toBe(105);
  });

  it("re-cuts from the next round when the clock is mid-rest", () => {
    const after = reviseTimeline(running(), 25, {
      ...settings,
      workSeconds: 45,
      restSeconds: 5,
    });

    expect(shape(after)).toEqual([
      [1, "work", 20],
      [1, "rest", 10], // still resting in it; it plays out as set
      [2, "work", 45],
      [2, "rest", 5],
      [3, "work", 45],
      [3, "rest", 5],
      [4, "work", 45],
    ]);
  });

  it("adds rounds on the end without disturbing the ones behind", () => {
    const after = reviseTimeline(running(), 35, { ...settings, rounds: 6 });

    expect(stateAt(after, 35)).toEqual(stateAt(running(), 35));
    // Round 4 used to be last, so it had no rest after it. It does now.
    expect(shape(after).slice(-5)).toEqual([
      [4, "work", 20],
      [4, "rest", 10],
      [5, "work", 20],
      [5, "rest", 10],
      [6, "work", 20],
    ]);
  });

  it("drops the rounds still to come when the count shrinks", () => {
    const after = reviseTimeline(running(), 65, { ...settings, rounds: 3 });

    // Round 3 was under way, so it finishes -- without the trailing rest that
    // only belonged there because a round 4 used to follow.
    expect(shape(after)).toEqual([
      [1, "work", 20],
      [1, "rest", 10],
      [2, "work", 20],
      [2, "rest", 10],
      [3, "work", 20],
    ]);
    expect(after.totalDuration).toBe(80);
    expect(stateAt(after, 80).finished).toBe(true);
  });

  it("ends after the round in progress when the count drops below it", () => {
    // Three rounds in, told to do one. The two already banked can't be undone.
    const after = reviseTimeline(running(), 65, { ...settings, rounds: 1 });

    expect(shape(after).at(-1)).toEqual([3, "work", 20]);
    expect(after.totalDuration).toBe(80);
  });

  it("switches timer type from the next round on", () => {
    const after = reviseTimeline(running(), 35, {
      ...settings,
      mode: "emom",
      intervalSeconds: 90,
    });

    expect(after.mode).toBe("emom");
    expect(shape(after)).toEqual([
      [1, "work", 20],
      [1, "rest", 10],
      [2, "work", 20], // the round under way plays out as the work it is
      // ...but the rest it would have owed is a work/rest idea, and this is
      // not a work/rest workout any more. Cycles from here.
      [3, "work", 90],
      [4, "work", 90],
    ]);
  });

  it("rebuilds from scratch before the clock has moved", () => {
    // Nothing has been played yet -- during the prep countdown, say -- so
    // there is no round in progress to protect.
    const after = reviseTimeline(running(), 0, { ...settings, workSeconds: 45 });

    expect(after).toEqual(buildTimeline({ ...settings, workSeconds: 45 }));
  });

  it("rebuilds from scratch once the workout is over", () => {
    const after = reviseTimeline(running(), 110, { ...settings, rounds: 2 });

    expect(after).toEqual(buildTimeline({ ...settings, rounds: 2 }));
  });

  it("survives an EMOM cycle change without moving the cycle it is in", () => {
    const before = emom({ rounds: 3, intervalSeconds: 60 });
    const after = reviseTimeline(before, 90, {
      mode: "emom",
      rounds: 3,
      workSeconds: 30,
      restSeconds: 30,
      intervalSeconds: 120,
    });

    expect(stateAt(after, 90)).toEqual(stateAt(before, 90)); // round 2, 30 left
    expect(shape(after)).toEqual([
      [1, "work", 60],
      [2, "work", 60],
      [3, "work", 120],
    ]);
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

  it("hands a new cycle length to the cycles still to come", () => {
    const ui = startEmom({ rounds: 2, interval: 4, prep: 1 });
    vi.advanceTimersByTime(1000);
    expect(ui.face()).toBe("00:03");

    ui.set("interval", 10); // second thoughts, mid-cycle

    expect(ui.face()).toBe("00:03"); // still on the cycle it started
    vi.advanceTimersByTime(3000);
    expect(ui.round()).toBe("2");
    expect(ui.face()).toBe("00:10"); // ...and cycle 2 runs on the new one
  });

  it("adds cycles mid-workout without moving the one in progress", () => {
    const ui = startEmom({ rounds: 2, interval: 4, prep: 1 });
    vi.advanceTimersByTime(1000);
    expect(ui.totalRounds()).toBe("2");

    ui.set("rounds", 5);

    expect(ui.face()).toBe("00:03");
    expect(ui.round()).toBe("1");
    expect(ui.totalRounds()).toBe("5");
    expect(ui.bars()).toHaveLength(5);

    // The three added cycles really do run: the workout no longer ends at 8s.
    vi.advanceTimersByTime(8000);
    expect(ui.round()).toBe("3");
    expect(ui.content.classList.contains("finished")).toBe(false);
  });

  it("edits during the prep countdown without swallowing the count", () => {
    const ui = mountTimer();
    ui.chooseMode("emom");
    ui.set("rounds", 2);
    ui.set("interval", 4);
    ui.set("prep", 5);
    ui.start();
    vi.advanceTimersByTime(2000);
    expect(ui.face()).toBe("00:03"); // three seconds of prep left

    ui.set("rounds", 4);

    expect(ui.face()).toBe("00:03"); // the prep count still owns the face
    expect(ui.totalRounds()).toBe("4");
    expect(ui.bars()).toHaveLength(4);

    ui.start(); // pause, still mid-count
    ui.set("rounds", 3);
    expect(ui.face()).toBe("00:03"); // and it still owns it while paused
    expect(ui.totalRounds()).toBe("3");
    ui.start(); // resume the countdown

    // Not a second has been played, so the edits landed whole: three cycles.
    vi.advanceTimersByTime(3000 + 12000);
    expect(ui.content.classList.contains("finished")).toBe(true);
    expect(ui.round()).toBe("3");
  });

  it("holds the timeline steady while a field is half-typed", () => {
    const ui = startEmom({ rounds: 2, interval: 4, prep: 1 });
    vi.advanceTimersByTime(1000);

    ui.set("rounds", ""); // mid-keystroke: no usable number yet
    ui.set("interval", "");

    expect(ui.totalRounds()).toBe("2");
    expect(ui.bars()).toHaveLength(2);
    vi.advanceTimersByTime(3000);
    expect(ui.round()).toBe("2"); // still running the workout it started
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

  it("takes edits made while paused, and resumes where it left off", () => {
    const ui = startEmom({ rounds: 3, interval: 4, prep: 1 });
    vi.advanceTimersByTime(2000); // two seconds into cycle 1
    expect(ui.face()).toBe("00:02");

    ui.start(); // the Start button is a Pause button now
    expect(ui.startLabel()).toBe("Resume");

    ui.set("interval", 60); // pausing to fix a number is the obvious way in
    expect(ui.face()).toBe("00:02"); // the paused cycle keeps its place

    ui.start();
    vi.advanceTimersByTime(2000); // cycle 1 runs out on its original length
    expect(ui.round()).toBe("2");
    expect(ui.face()).toBe("01:00"); // cycle 2 on the edited length
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

  // The issue's own example: three rounds in, the rest turns out to be too
  // long and the round count too short. Neither correction may move the clock.
  it("re-cuts the rounds still to come without moving the clock", () => {
    const ui = mountTimer();
    ui.set("rounds", 2);
    ui.set("work", 3);
    ui.set("rest", 2);
    ui.set("prep", 1);
    ui.start();
    vi.advanceTimersByTime(1000); // prep done
    vi.advanceTimersByTime(4000); // four seconds in: round 1's rest

    expect(ui.face()).toBe("00:04");
    expect(ui.content.classList.contains("resting")).toBe(true);

    ui.set("rounds", 4);
    ui.set("rest", 1);

    expect(ui.face()).toBe("00:04"); // same second of the same rest
    expect(ui.round()).toBe("1");
    expect(ui.content.classList.contains("resting")).toBe(true);
    expect(ui.totalRounds()).toBe("4");
    expect(ui.bars()).toHaveLength(7); // w r w r w r w

    vi.advanceTimersByTime(1000); // the rest it was already in plays out at 2s
    expect(ui.round()).toBe("2");
    expect(ui.content.classList.contains("working")).toBe(true);

    vi.advanceTimersByTime(3000); // round 2's work done
    expect(ui.content.classList.contains("resting")).toBe(true);
    vi.advanceTimersByTime(1000); // ...and its rest is the new, shorter one
    expect(ui.round()).toBe("3");
    expect(ui.content.classList.contains("working")).toBe(true);
  });

  it("switches timer type from the next round on", () => {
    const ui = mountTimer();
    ui.set("rounds", 2);
    ui.set("work", 3);
    ui.set("rest", 2);
    ui.set("prep", 1);
    ui.set("interval", 5);
    ui.start();
    vi.advanceTimersByTime(2000); // prep, then one second of round 1's work

    ui.chooseMode("emom");

    // The face flips to the EMOM countdown, still inside round 1's work: two
    // of its three seconds left.
    expect(ui.face()).toBe("00:02");
    expect(ui.round()).toBe("1");

    vi.advanceTimersByTime(2000); // round 1 ends on its own terms
    expect(ui.round()).toBe("2");
    expect(ui.face()).toBe("00:05"); // and round 2 is a cycle, not work + rest
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
