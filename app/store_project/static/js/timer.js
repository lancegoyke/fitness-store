/*
 * Fitness Interval Timer
 * Author: Lance Goyke
 * Site: https://mastering.fitness/timer/
 *
 */

// DOM Elements
const countdown = document.querySelector("#countdown");
const countdownMinutes = document.querySelector("#minutes");
const countdownSeconds = document.querySelector("#seconds");
const cycles = document.querySelector("#cycles");
const form = document.querySelector("#timer-form");
const startButton = document.querySelector(".start");
const resetButton = document.querySelector(".reset");
const currentRoundElement = document.querySelector("#current-round");
const totalRoundsElement = document.querySelector("#total-rounds");
const content = document.querySelector(".content");
const roundsInput = form.querySelector("#rounds");
const workInput = form.querySelector("#work");
const restInput = form.querySelector("#rest");
const prepInput = form.querySelector("#prep");

// Global Variables
let timer;
let prepTimer;
let prepCounter;
let totalRoundSeconds;
let elapsedSeconds = 1;
let currentRound = 1;
let isResting = false;
let isPaused = false;
let rounds = parseInt(roundsInput.value);
let workSeconds = parseInt(workInput.value);
let restSeconds = parseInt(restInput.value);
let prepSeconds = parseInt(prepInput.value);

if (audio == undefined) {
  console.log("Audio not loaded. Timer will be silent.");
} else {
  console.log("Audio loaded.");
}

// Event Listeners
roundsInput.addEventListener("input", render);
workInput.addEventListener("input", render);
restInput.addEventListener("input", render);
prepInput.addEventListener("input", render);
form.addEventListener("submit", startTimer);
resetButton.addEventListener("click", resetTimer);

// Set initial state
render();

/*
/* Utility Functions
*/
function clearTimers() {
  clearInterval(timer);
  clearInterval(prepTimer);
  timer = null;
  prepTimer = null;
}

function getMinutes(seconds) {
  return `${Math.floor(seconds / 60)}`.padStart(2, "0");
}

function getSeconds(seconds) {
  return `${seconds % 60}`.padStart(2, "0");
}

/*
/* Display Functions
*/
function createProgressBars() {
  const totalDuration = rounds * (workSeconds + restSeconds) - restSeconds;
  const progressContainer = document.querySelector("#progress-container");
  progressContainer.innerHTML = "";
  const progressOverlay = document.querySelector("#progress-overlay");
  progressOverlay.innerHTML = "";

  for (let i = 1; i <= rounds; i++) {
    const workBar = document.createElement("div");
    workBar.className = "progress-bar progress-bar-success";
    workBar.style.width = `${(workSeconds / totalDuration) * 100}%`;
    workBar.textContent = i;
    progressContainer.appendChild(workBar);

    if (i !== rounds) {
      const restBar = document.createElement("div");
      restBar.className = "progress-bar progress-bar-danger";
      restBar.style.width = `${(restSeconds / totalDuration) * 100}%`;
      progressContainer.appendChild(restBar);
    }
  }
  const elapsedBar = document.createElement("div");
  elapsedBar.className = "progress-bar progress-bar-elapsed";
  elapsedBar.style.width = "0%";
  progressOverlay.appendChild(elapsedBar);
}

function render() {
  // Get new values
  rounds = parseInt(form.querySelector("#rounds").value);
  workSeconds = parseInt(form.querySelector("#work").value);
  restSeconds = parseInt(form.querySelector("#rest").value);

  // Set new values
  minutes.innerHTML = getMinutes(workSeconds);
  seconds.innerHTML = getSeconds(workSeconds);
  currentRoundElement.innerHTML = 1;
  totalRoundsElement.innerHTML = rounds;

  // Create progress bars
  createProgressBars();
}

/*
/* Timer Functions
*/
function startTimer(e) {
  e.preventDefault();
  console.log("Starting timer");
  console.log("isPaused: ", isPaused);

  clearTimers();

  // Make timer pauseable
  startButton.innerHTML = "Pause";
  form.removeEventListener("submit", startTimer);
  form.addEventListener("submit", pauseTimer);

  // Start preparation countdown
  if (!isPaused) {
    prepSeconds = parseInt(prepInput.value);
    prepCounter = prepSeconds;
    countdownMinutes.innerHTML = getMinutes(prepCounter);
    countdownSeconds.innerHTML = getSeconds(prepCounter);
    content.classList.add("preparing");

    // Update the preparation countdown every second
    prepTimer = setInterval(() => {
      prepCounter--;
      countdownMinutes.innerHTML = getMinutes(prepCounter);
      countdownSeconds.innerHTML = getSeconds(prepCounter);

      if (prepCounter === 3) {
        audio["three"].play();
      }
      if (prepCounter === 2) {
        audio["two"].play();
      }
      if (prepCounter === 1) {
        audio["one"].play();
      }
      if (prepCounter === 0) {
        audio["go"].play();
        clearInterval(prepTimer);
        startWorkout();
      }
    }, 1000);
  } else {
    // Resuming a paused timer
    if (prepCounter > 0) {
      prepTimer = setInterval(() => {
        prepCounter--;
        countdownMinutes.innerHTML = getMinutes(prepCounter);
        countdownSeconds.innerHTML = getSeconds(prepCounter);

        if (prepCounter === 0) {
          audio["go"].play();
          clearInterval(prepTimer);
          startWorkout();
        }
      }, 1000);
    } else {
      clearInterval(prepTimer);
      startWorkout();
    }
  }
}

function startWorkout() {
  // Update the DOM
  rounds = parseInt(form.querySelector("#rounds").value);
  workSeconds = parseInt(form.querySelector("#work").value);
  restSeconds = parseInt(form.querySelector("#rest").value);
  // audioCue
  content.classList.remove("preparing");
  content.classList.add("working");
  const elapsedBar = document.querySelector(".progress-bar-elapsed");

  // Set initial values
  isResting = false;
  if (!isPaused) {
    minutes.innerHTML = getMinutes(0);
    seconds.innerHTML = getSeconds(0);
    totalRoundsElement.innerHTML = rounds;
  } else {
    isPaused = false;
  }
  totalRoundSeconds = workSeconds + restSeconds;
  totalDuration = rounds * (workSeconds + restSeconds) - restSeconds;

  // Update the display every 1000 milliseconds
  timer = setInterval(() => {
    // Waits 1 second before running
    minutes.innerHTML = getMinutes(elapsedSeconds);
    seconds.innerHTML = getSeconds(elapsedSeconds);

    secondsLeftInRound =
      totalRoundSeconds - (elapsedSeconds % totalRoundSeconds);

    if (currentRound === rounds && secondsLeftInRound === restSeconds) {
      // We finished the last round
      audio["rest"].play();
      console.log("Confetti!");
      clearInterval(timer);
      content.classList.remove("working");
      content.classList.remove("resting");
      content.classList.add("finished");
    } else if (secondsLeftInRound === totalRoundSeconds) {
      // We just started a new round
      audio["go"].play();
      currentRound++;
      currentRoundElement.innerHTML = currentRound;
      isResting = !isResting;
      content.classList.add("working");
      content.classList.remove("resting");
    } else if (secondsLeftInRound === restSeconds) {
      // We finished working and now we're resting
      audio["rest"].play();
      isResting = !isResting;
      content.classList.remove("working");
      content.classList.add("resting");
    } else if (
      (secondsLeftInRound === 3) |
      (secondsLeftInRound - restSeconds === 3)
    ) {
      audio["three"].play();
    } else if (
      (secondsLeftInRound === 2) |
      (secondsLeftInRound - restSeconds === 2)
    ) {
      audio["two"].play();
    } else if (
      (secondsLeftInRound === 1) |
      (secondsLeftInRound - restSeconds === 1)
    ) {
      audio["one"].play();
    }

    // Update the progress bar
    elapsedBar.style.width = `${(elapsedSeconds / totalDuration) * 100}%`;

    elapsedSeconds++;
  }, 1000);
}

function pauseTimer(e) {
  e.preventDefault();
  console.log("Pausing timer");
  console.log(`Elapsed seconds: ${elapsedSeconds}`);
  isPaused = true;
  console.log(`isPaused: ${isPaused}`);
  clearTimers();

  // Change the button from pause to resume
  startButton.innerHTML = "Resume";
  form.removeEventListener("submit", pauseTimer);
  form.addEventListener("submit", startTimer);
}

function resetTimer(e) {
  e.preventDefault();

  // Reset timers
  clearTimers();
  isPaused = false;
  prepSeconds = parseInt(prepInput.value);
  currentRound = 1;
  elapsedSeconds = 1;

  // Reset the DOM
  render();
  startButton.innerHTML = "Start";
  form.removeEventListener("submit", pauseTimer);
  form.addEventListener("submit", startTimer);
  content.classList.remove("preparing");
  content.classList.remove("working");
  content.classList.remove("resting");
  content.classList.remove("finished");

  console.log("Reset timer");
}
