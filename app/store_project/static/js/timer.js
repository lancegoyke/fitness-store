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
 * "what should I play?" (cueAt). A form edit mid-workout re-cuts the timeline
 * ahead of the clock (reviseTimeline) without touching a second of what has
 * already been played. All of these are pure and are unit tested in
 * frontend/timer.test.js.
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

// Lay segments end to end, stamping each with the second it starts on and the
// second it ends on -- the form the clock reads.
function stampTimeline(mode, segments) {
  let elapsed = 0;
  for (const segment of segments) {
    segment.start = elapsed;
    elapsed += segment.seconds;
    segment.end = elapsed;
  }
  return { mode, segments, totalDuration: elapsed };
}

// Everything left to play after `previous`, cut from the given settings.
// Segments with no duration are skipped, so a rest of 0 collapses into a
// straight repeat. `previous` describes the round already under way: which
// number it carries, and whether it still owes a rest before the next round
// can start.
function segmentsAfter(previous, config) {
  const { mode, rounds, workSeconds, restSeconds, intervalSeconds } = config;
  const segments = [];

  function push(round, kind, seconds) {
    if (seconds > 0) segments.push({ round, kind, seconds });
  }

  if (mode === MODE_EMOM) {
    for (let round = previous.round + 1; round <= rounds; round++) {
      push(round, "work", intervalSeconds);
    }
    return segments;
  }

  // Pay off the rest the round under way still owes -- unless it is now the
  // last round, where the trailing rest is dropped so the workout ends on work.
  if (previous.owesRest && previous.round < rounds) {
    push(previous.round, "rest", restSeconds);
  }
  for (let round = previous.round + 1; round <= rounds; round++) {
    push(round, "work", workSeconds);
    if (round !== rounds) push(round, "rest", restSeconds);
  }
  return segments;
}

// Compile the form's settings into a timeline. A fresh workout is just
// "everything after a round zero that owes nothing" -- sharing one cut with
// reviseTimeline below, so a workout can't come out shaped one way on Start
// and another way after an edit.
function buildTimeline(config) {
  return stampTimeline(
    config.mode,
    segmentsAfter({ round: 0, owesRest: false }, config)
  );
}

// Re-cut a workout that is already under way against edited settings
// (issue #496). What has been played is history: the segments behind
// `position` and the one the clock is standing in are carried over verbatim,
// and the new settings take over from the next segment on. Nothing at or
// before the clock ever moves, so an edit can't drop the athlete into a
// different part of the workout -- the rounds already banked stay banked, and
// the round in progress plays out as it was started, from its first second on.
function reviseTimeline(timeline, position, config) {
  const current = segmentAt(timeline, position);

  // The workout is over: no round left to protect, so the edit lands whole.
  if (!current) return buildTimeline(config);

  const played = timeline.segments.slice(
    0,
    timeline.segments.indexOf(current) + 1
  );

  return stampTimeline(config.mode, [
    ...played.map((segment) => ({ ...segment })),
    // An EMOM cycle is a whole round, self-paced work and its own rest in one
    // segment -- so unlike a work/rest work phase, it owes nothing on the way
    // out and the next round starts clean.
    ...segmentsAfter(
      {
        round: current.round,
        owesRest: timeline.mode !== MODE_EMOM && current.kind === "work",
      },
      config
    ),
  ]);
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
    handleEdit();
  });
  roundsInput.addEventListener("input", handleEdit);
  workInput.addEventListener("input", handleEdit);
  restInput.addEventListener("input", handleEdit);
  intervalInput.addEventListener("input", handleEdit);
  prepInput.addEventListener("input", handleEdit);
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

  // The round count the face advertises: the last round the timeline actually
  // reaches. Mid-workout that can sit below the number in the box -- winding a
  // 10-round workout back to 2 on round 6 finishes round 6, it can't unspend
  // the five before it. Falls back to the box when there is nothing to run, so
  // an unusable form still previews the count it is aiming at.
  function showTotalRounds() {
    const last = timeline.segments[timeline.segments.length - 1];
    totalRoundsElement.innerHTML = last ? last.round : toInt(roundsInput.value);
  }

  // The clock's position: the second currently on the face. The interval has
  // already queued the second after it, which is what `elapsedSeconds` holds.
  function clockPosition() {
    return elapsedSeconds - 1;
  }

  // A form edit. Idle, it re-previews the workout from scratch. Mid-workout --
  // running or paused -- it re-cuts only the rounds still to come, so the
  // round in progress and every round already banked stay exactly as played
  // (issue #496).
  function handleEdit() {
    if (!timer && !prepTimer && !isPaused) {
      render();
      return;
    }

    // Half-typed is not an instruction. A blank rounds box or an out-of-range
    // duration would otherwise re-cut the workout on every keystroke of a
    // number the athlete is still in the middle of typing. (Constraint
    // validation still answers here even though submit-time validation is off
    // while the clock runs, and it skips the disabled fields of the other
    // timer type.)
    if (!form.checkValidity()) return;

    const position = clockPosition();

    // While the prep countdown still owes seconds -- ticking, or paused
    // mid-count -- not one second of the workout has been played, so there is
    // no round to protect and the edit lands whole. It also still owns the
    // face: painting the workout's first second over it would swallow the
    // count. Once it hands over at zero the workout is under way, first second
    // included, and edits go to the rounds still to come.
    const counting = prepCounter > 0;
    timeline = counting
      ? buildTimeline(readConfig())
      : reviseTimeline(timeline, position, readConfig());

    createProgressBars();
    showTotalRounds();
    if (!counting) paint(position);
  }

  function render() {
    timeline = buildTimeline(readConfig());

    // Preview the first segment: the work duration, or the EMOM cycle.
    showTime(stateAt(timeline, 0).secondsLeft);
    currentRoundElement.innerHTML = 1;
    showTotalRounds();

    createProgressBars();
  }

  /*
  /* Timer Functions
  */
  // Start, Pause and Resume are all the same submit button. Native validation
  // is welcome on a fresh Start -- that's the one press that reads the form,
  // and how a blank field gets its "fill this out" prompt. It must be off for
  // the others: they act on the timeline already running, so a half-typed
  // duration would block the submit event and leave the button dead.
  function makePauseable() {
    startButton.innerHTML = "Pause";
    form.noValidate = true;
    form.removeEventListener("submit", startTimer);
    form.addEventListener("submit", pauseTimer);
  }

  function makeStartable(label) {
    startButton.innerHTML = label;
    form.noValidate = isPaused; // paused means the next press is a Resume
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
      showTotalRounds();
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
    const resumed = paint(clockPosition());
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
    reviseTimeline,
    segmentAt,
    stateAt,
    cueAt,
    getMinutes,
    getSeconds,
  };
}
