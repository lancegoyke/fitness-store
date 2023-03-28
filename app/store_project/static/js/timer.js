/*
 * Fitness Interval Timer
 * Author: Lance Goyke
 *
 * TODO
 * - [ ] Add audio cues
 */

// DOM Elements
let timer;
let elapsedSeconds;
let totalRoundSeconds;
let currentRound;
let isResting;
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

// Computation Values
let rounds = parseInt(form.querySelector("#rounds").value);
let workSeconds = parseInt(form.querySelector("#work").value);
let restSeconds = parseInt(form.querySelector("#rest").value);

// Event Listener
form.addEventListener("focusout", () => {
  render();
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  startTimer();
});

resetButton.addEventListener("click", () => {
  resetTimer();
});

// Set initial state
render();

// Functions
function render() {
  // Get new values
  rounds = parseInt(form.querySelector("#rounds").value);
  workSeconds = parseInt(form.querySelector("#work").value);
  restSeconds = parseInt(form.querySelector("#rest").value);

  // Set new values
  minutes.innerHTML = `${Math.floor(workSeconds / 60)}`.padStart(2, "0");
  seconds.innerHTML = `${workSeconds % 60}`.padStart(2, "0");
  currentRoundElement.innerHTML = 1;
  totalRoundsElement.innerHTML = rounds;

  // Create progress bars
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

function startTimer() {
  if (timer) {
    clearInterval(timer);
  }

  // Update the DOM
  rounds = parseInt(form.querySelector("#rounds").value);
  workSeconds = parseInt(form.querySelector("#work").value);
  restSeconds = parseInt(form.querySelector("#rest").value);
  // audioCue
  content.classList.add("working");
  const elapsedBar = document.querySelector(".progress-bar-elapsed");

  // Change the button from start to pause
  // startButton.innerHTML = "Pause";

  // Set initial values
  isResting = false;
  currentRound = 1;
  minutes.innerHTML = "0".padStart(2, "0");
  seconds.innerHTML = "0".padStart(2, "0");
  totalRoundsElement.innerHTML = rounds;
  elapsedSeconds = 1;
  totalRoundSeconds = workSeconds + restSeconds;
  totalDuration = rounds * (workSeconds + restSeconds) - restSeconds;

  // Update the display every 1000 milliseconds
  timer = setInterval(() => {
    // Waits 1 second before running
    minutes.innerHTML = `${Math.floor(elapsedSeconds / 60)}`.padStart(2, "0");
    seconds.innerHTML = `${elapsedSeconds % 60}`.padStart(2, "0");

    secondsLeftInRound =
      totalRoundSeconds - (elapsedSeconds % totalRoundSeconds);

    if (currentRound === rounds && secondsLeftInRound === restSeconds) {
      // We finished the last round
      console.log("Confetti!");
      clearInterval(timer);
      content.classList.remove("working");
      content.classList.remove("resting");
      content.classList.add("finished");
    } else if (secondsLeftInRound === totalRoundSeconds) {
      // We just started a new round
      currentRound++;
      currentRoundElement.innerHTML = currentRound;
      isResting = !isResting;
      content.classList.add("working");
      content.classList.remove("resting");
    } else if (secondsLeftInRound === restSeconds) {
      // We finished working and now we're resting
      isResting = !isResting;
      content.classList.remove("working");
      content.classList.add("resting");
    }

    // Update the progress bar
    elapsedBar.style.width = `${(elapsedSeconds / totalDuration) * 100}%`;
    console.log(`${(elapsedSeconds / totalDuration) * 100}%`);
    console.log(elapsedBar);

    elapsedSeconds++;
  }, 1000);
}

function resetTimer() {
  clearInterval(timer);
  timer = null;
  currentRound = 1;
  elapsedSeconds = 0;
  render();
  content.classList.remove("working");
  content.classList.remove("resting");
  content.classList.remove("finished");
}
