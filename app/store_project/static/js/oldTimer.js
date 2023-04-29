// Variables
let currentRound = 0;
let timer;
let timeLeft;
let work;
let totalDuration;
let audioElement;

// DOM Elements
const timerForm = document.querySelector('#timer-form');
const timerDisplay = document.querySelector('#timer-display');
const status = document.querySelector('#status');
const timerElement = document.querySelector('#timer');
const progressBar = document.querySelector('#progress-bar');
const startButton = document.querySelector('.start');
const resetButton = document.querySelector('.reset');

// Computation Values
let rounds = parseInt(timerForm.querySelector('#rounds').value);
let workDuration = parseInt(timerForm.querySelector('#work-duration').value);
let restDuration = parseInt(timerForm.querySelector('#rest-duration').value);
createProgressBars(rounds, workDuration, restDuration);

// Event Listeners
timerForm.addEventListener('submit', (e) => {
  e.preventDefault();
  startTimer();
});

timerForm.addEventListener('focusout', (e) => {
  rounds = parseInt(timerForm.querySelector('#rounds').value);
  workDuration = parseInt(timerForm.querySelector('#work-duration').value);
  restDuration = parseInt(timerForm.querySelector('#rest-duration').value);
  createProgressBars(rounds, workDuration, restDuration);
});

resetButton.addEventListener('click', () => {
  resetTimer();
});

// Functions
function createProgressBars(rounds, workDuration, restDuration) {
  const totalDuration = rounds * (workDuration + restDuration) - restDuration;

  const progressContainer = document.querySelector('#progress-container');
  progressContainer.innerHTML = '';
  const progressOverlay = document.querySelector('#progress-overlay');
  progressOverlay.innerHTML = '';

  for (let i = 1; i <= rounds; i++) {
    const workBar = document.createElement('div');
    workBar.className = 'progress-bar progress-bar-success';
    workBar.style.width = `${(workDuration / totalDuration) * 100}%`;
    workBar.textContent = i;
    progressContainer.appendChild(workBar);

    if (i !== rounds) {
      const restBar = document.createElement('div');
      restBar.className = 'progress-bar progress-bar-danger';
      restBar.style.width = `${(restDuration / totalDuration) * 100}%`;
      progressContainer.appendChild(restBar);
    }
  }
  const elapsedBar = document.createElement('div');
  elapsedBar.className = 'progress-bar progress-bar-elapsed';
  elapsedBar.style.width = '0%';
  
}

function startTimer() {
  if (timer) {
    clearInterval(timer);
  }

  const rounds = parseInt(document.getElementById('rounds').value);
  const workDuration = parseInt(document.getElementById('work-duration').value);
  const restDuration = parseInt(document.getElementById('rest-duration').value);
  const audioCue = document.getElementById('audio-cue').value;

  currentRound = 1;
  work = true;
  totalDuration = rounds * (workDuration + restDuration) - restDuration;
  timeLeft = workDuration;
  updateTimer();

  // Load audio
  audioElement = new Audio(`${audioCue}.mp3`);

  // Show timer display and hide form
  timerForm.classList.add('hidden');
  timerDisplay.classList.remove('hidden');

  // Create progress bars
  createProgressBars(rounds, workDuration, restDuration);

  // Change the start button to a stop button
  startButton.textContent = 'Stop';
  startButton.classList.add('stop');

  timer = setInterval(() => {
    timeLeft--;

    if (timeLeft < 0) {
      // Play audio cue
      audioElement.play();

      work = !work;
      status.className = work ? 'work' : 'rest';
      timeLeft = work ? workDuration : restDuration;

      if (!work) {
        currentRound++;
        if (currentRound > rounds) {
          clearInterval(timer);
          resetTimer();
        }
      }
    }

    updateTimer();
  }, 1000);
}


function updateTimer() {
  const workDuration = parseInt(document.querySelector('#work-duration').value);
  const restDuration = parseInt(document.querySelector('#rest-duration').value);
  const rounds = parseInt(document.querySelector('#rounds').value);

  // Calculate the total time for all rounds without final rest
  const totalTime = rounds * (workDuration + restDuration) - restDuration;

  // Calculate the time elapsed so far
  const currentRoundDuration = work ? workDuration - timeLeft : workDuration + (restDuration - timeLeft);
  const timeElapsed = (currentRound - 1) * (workDuration + restDuration) + currentRoundDuration;

  // Update the progress bar
  const bars = document.querySelectorAll('#progress-container .progress-bar');
  const elapsedBars = Math.floor(timeElapsed / (workDuration + restDuration)) * 2;
  for (let i = 0; i < bars.length; i++) {
    if (i < elapsedBars) {
      bars[i].style.width = '100%';
      bars[i].classList.add('progress-bar-elapsed');
    } else if (i === elapsedBars) {
      const fraction = (timeElapsed % (workDuration + restDuration)) / (workDuration + restDuration);
      bars[i].style.width = `${fraction * 100}%`;
      bars[i].classList.add('progress-bar-elapsed');
    } else {
      bars[i].style.width = '0%';
      bars[i].classList.remove('progress-bar-elapsed');
    }
  }

  timerElement.textContent = `${timeLeft}s`;
}

function resetTimer() {
  clearInterval(timer);
  timer = null;
}
