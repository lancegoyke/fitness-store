/*
 * Fitness Interval Timer
 * Author: Lance Goyke
 * Site: https://mastering.fitness/timer/
 *
 * Two timer types share one engine:
 *
 *   work-rest  Alternating work and rest, the classic tabata shape. The
 *              trailing rest is dropped so the workout ends on work.
 *   emom       Every-X-on-the-X. There is no explicit rest: each round is one
 *              repeat cycle, the work is self-paced, and whatever is left of
 *              the cycle is the rest. The cycle length is configurable, so
 *              this covers E2MOM, E90s, and friends -- not just the minute.
 *
 * Both are compiled up front into a timeline of segments. The ticking clock
 * only ever asks the timeline two questions: "where am I?" (stateAt) and
 * "what should I play?" (cueAt). Those three functions are pure and are unit
 * tested in frontend/timer.test.js.
 */

const MODE_WORK_REST = "work-rest";
const MODE_EMOM = "emom";

/*
/* Utility Functions
*/
function getMinutes(seconds) {
  return `${Math.floor(seconds / 60)}`.padStart(2, "0");
}

function getSeconds(seconds) {
  return `${seconds % 60}`.padStart(2, "0");
}

function toInt(value) {
  const parsed = parseInt(value, 10);
  return Number.isNaN(parsed) ? 0 : parsed;
}

/*
/* Timeline Functions (pure)
*/

// Compile the form's settings into an ordered list of segments, each stamped
// with the second it starts on and the second it ends on. Segments with no
// duration are skipped, so a rest of 0 collapses into a straight repeat.
function buildTimeline({
  mode,
  rounds,
  workSeconds,
  restSeconds,
  intervalSeconds,
}) {
  const segments = [];

  function push(round, kind, seconds) {
    if (seconds > 0) segments.push({ round, kind, seconds });
  }

  for (let round = 1; round <= rounds; round++) {
    if (mode === MODE_EMOM) {
      push(round, "work", intervalSeconds);
    } else {
      push(round, "work", workSeconds);
      if (round !== rounds) push(round, "rest", restSeconds);
    }
  }

  let elapsed = 0;
  for (const segment of segments) {
    segment.start = elapsed;
    elapsed += segment.seconds;
    segment.end = elapsed;
  }

  return { mode, segments, totalDuration: elapsed };
}

// The segment covering `elapsedSeconds`, or null once the workout is over.
function segmentAt(timeline, elapsedSeconds) {
  return (
    timeline.segments.find((segment) => elapsedSeconds < segment.end) || null
  );
}

function stateAt(timeline, elapsedSeconds) {
  const current = segmentAt(timeline, elapsedSeconds);
  if (!current) {
    const last = timeline.segments[timeline.segments.length - 1];
    return {
      round: last ? last.round : 1,
      kind: "finished",
      secondsLeft: 0,
      finished: true,
    };
  }
  return {
    round: current.round,
    kind: current.kind,
    secondsLeft: current.end - elapsedSeconds,
    finished: false,
  };
}

// The audio cue owed at `elapsedSeconds`, or null for a silent second. The
// second a segment starts on gets its opening cue; the three seconds before a
// segment ends get the 3-2-1 countdown into it.
function cueAt(timeline, elapsedSeconds) {
  const current = segmentAt(timeline, elapsedSeconds);
  if (!current) return "rest"; // the workout just ended
  if (elapsedSeconds > 0 && elapsedSeconds === current.start) {
    return current.kind === "work" ? "go" : "rest";
  }
  const secondsLeft = current.end - elapsedSeconds;
  if (secondsLeft === 3) return "three";
  if (secondsLeft === 2) return "two";
  if (secondsLeft === 1) return "one";
  return null;
}

/*
/* DOM Controller
*/
function initTimer() {
  const form = document.querySelector("#timer-form");
  if (!form) return;

  const countdownMinutes = document.querySelector("#minutes");
  const countdownSeconds = document.querySelector("#seconds");
  const currentRoundElement = document.querySelector("#current-round");
  const totalRoundsElement = document.querySelector("#total-rounds");
  const progressContainer = document.querySelector("#progress-container");
  const progressOverlay = document.querySelector("#progress-overlay");
  const content = document.querySelector(".content");
  const startButton = form.querySelector(".start");
  const resetButton = form.querySelector(".reset");
  const modeInput = form.querySelector("#mode");
  const roundsInput = form.querySelector("#rounds");
  const workInput = form.querySelector("#work");
  const restInput = form.querySelector("#rest");
  const intervalInput = form.querySelector("#interval");
  const prepInput = form.querySelector("#prep");

  // The audio bank is declared by the template, ahead of this script.
  const cues = typeof audio === "undefined" ? null : audio;
  if (cues) {
    console.log("Audio loaded.");
  } else {
    console.log("Audio not loaded. Timer will be silent.");
  }

  let timer;
  let prepTimer;
  let prepCounter;
  let elapsedSeconds = 1;
  let isPaused = false;
  let timeline = buildTimeline(readConfig());

  // Event Listeners
  modeInput.addEventListener("change", () => {
    applyMode();
    render();
  });
  roundsInput.addEventListener("input", render);
  workInput.addEventListener("input", render);
  restInput.addEventListener("input", render);
  intervalInput.addEventListener("input", render);
  prepInput.addEventListener("input", render);
  form.addEventListener("submit", startTimer);
  resetButton.addEventListener("click", resetTimer);

  // Set initial state
  applyMode();
  render();

  function clearTimers() {
    clearInterval(timer);
    clearInterval(prepTimer);
    timer = null;
    prepTimer = null;
  }

  function play(cue) {
    if (cue && cues && cues[cue]) cues[cue].play();
  }

  function readConfig() {
    return {
      mode: modeInput.value,
      rounds: toInt(roundsInput.value),
      workSeconds: toInt(workInput.value),
      restSeconds: toInt(restInput.value),
      intervalSeconds: toInt(intervalInput.value),
    };
  }

  function showTime(seconds) {
    countdownMinutes.innerHTML = getMinutes(seconds);
    countdownSeconds.innerHTML = getSeconds(seconds);
  }

  /*
  /* Display Functions
  */

  // Show only the fields the selected timer type uses. Hidden inputs are
  // disabled, not just un-required: a leftover out-of-range value would
  // otherwise fail validation from off-screen and silently swallow the submit,
  // making Start look broken. Disabled inputs are still readable by value.
  function applyMode() {
    const active = modeInput.value;
    for (const field of form.querySelectorAll("[data-mode]")) {
      const visible = field.dataset.mode === active;
      field.classList.toggle("hidden", !visible);
      for (const input of field.querySelectorAll("input")) {
        input.required = visible;
        input.disabled = !visible;
      }
    }
  }

  function createProgressBars() {
    progressContainer.innerHTML = "";
    progressOverlay.innerHTML = "";

    for (const segment of timeline.segments) {
      const bar = document.createElement("div");
      const tone = segment.kind === "work" ? "success" : "danger";
      bar.className = `progress-bar progress-bar-${tone}`;
      bar.style.width = `${(segment.seconds / timeline.totalDuration) * 100}%`;
      if (segment.kind === "work") bar.textContent = segment.round;
      progressContainer.appendChild(bar);
    }

    const elapsedBar = document.createElement("div");
    elapsedBar.className = "progress-bar progress-bar-elapsed";
    elapsedBar.style.width = "0%";
    progressOverlay.appendChild(elapsedBar);
  }

  // Draw the workout as it stands after `elapsed` seconds. Work/rest shows the
  // running total; EMOM shows the time left in the cycle, which is the number
  // you actually train off.
  function paint(elapsed) {
    const state = stateAt(timeline, elapsed);
    showTime(timeline.mode === MODE_EMOM ? state.secondsLeft : elapsed);
    currentRoundElement.innerHTML = state.round;

    const elapsedBar = document.querySelector(".progress-bar-elapsed");
    if (elapsedBar && timeline.totalDuration > 0) {
      const progress = Math.min(elapsed / timeline.totalDuration, 1);
      elapsedBar.style.width = `${progress * 100}%`;
    }
    return state;
  }

  function render() {
    // Editing the form mid-workout would swap the timeline out from under the
    // clock -- and a paused workout still has an `elapsedSeconds` to resume
    // against. Leave it be; the edits land on the next fresh start.
    if (timer || prepTimer || isPaused) return;

    timeline = buildTimeline(readConfig());

    // Preview the first segment: the work duration, or the EMOM cycle.
    showTime(stateAt(timeline, 0).secondsLeft);
    currentRoundElement.innerHTML = 1;
    totalRoundsElement.innerHTML = toInt(roundsInput.value);

    createProgressBars();
  }

  /*
  /* Timer Functions
  */
  function makePauseable() {
    startButton.innerHTML = "Pause";
    form.removeEventListener("submit", startTimer);
    form.addEventListener("submit", pauseTimer);
  }

  function makeStartable(label) {
    startButton.innerHTML = label;
    form.removeEventListener("submit", pauseTimer);
    form.addEventListener("submit", startTimer);
  }

  function startTimer(e) {
    e.preventDefault();
    clearTimers();

    if (!isPaused) {
      const next = buildTimeline(readConfig());
      if (next.totalDuration <= 0) return; // nothing to run; stay put

      timeline = next;
      elapsedSeconds = 1;
      prepCounter = toInt(prepInput.value);
      totalRoundsElement.innerHTML = toInt(roundsInput.value);
      createProgressBars();
      showTime(prepCounter);
      content.classList.remove("working", "resting", "finished");
      content.classList.add("preparing");
    }
    isPaused = false;
    makePauseable();

    if (prepCounter > 0) {
      runPrep();
    } else {
      startWorkout();
    }
  }

  function runPrep() {
    prepTimer = setInterval(() => {
      prepCounter--;
      showTime(prepCounter);

      if (prepCounter === 3) play("three");
      if (prepCounter === 2) play("two");
      if (prepCounter === 1) play("one");
      if (prepCounter === 0) {
        play("go");
        clearInterval(prepTimer);
        prepTimer = null;
        startWorkout();
      }
    }, 1000);
  }

  function startWorkout() {
    content.classList.remove("preparing");

    // Draw the second the clock is about to advance past, so a fresh start
    // opens on 00:00 (or a full EMOM cycle) instead of a blank face, and a
    // resume comes back on the segment it was paused in.
    const resumed = paint(elapsedSeconds - 1);
    content.classList.toggle("working", resumed.kind === "work");
    content.classList.toggle("resting", resumed.kind === "rest");

    timer = setInterval(() => {
      const state = paint(elapsedSeconds);
      play(cueAt(timeline, elapsedSeconds));

      if (state.finished) {
        console.log("Confetti!");
        clearTimers();
        content.classList.remove("working", "resting");
        content.classList.add("finished");
        makeStartable("Start");
        return;
      }

      content.classList.toggle("working", state.kind === "work");
      content.classList.toggle("resting", state.kind === "rest");

      elapsedSeconds++;
    }, 1000);
  }

  function pauseTimer(e) {
    e.preventDefault();
    isPaused = true;
    clearTimers();
    makeStartable("Resume");
  }

  function resetTimer(e) {
    e.preventDefault();

    clearTimers();
    isPaused = false;
    elapsedSeconds = 1;

    render();
    makeStartable("Start");
    content.classList.remove("preparing", "working", "resting", "finished");
  }
}

// Wire up the page. Guarded so Node-based test runners, which import this file
// for the pure timeline helpers, don't try to build a timer without a form.
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTimer);
  } else {
    initTimer();
  }
}

// Test hook: expose the timeline engine to Node-based runners (vitest).
// Skipped in the browser, where `module` is undefined.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    MODE_WORK_REST,
    MODE_EMOM,
    initTimer,
    buildTimeline,
    segmentAt,
    stateAt,
    cueAt,
    getMinutes,
    getSeconds,
  };
}
