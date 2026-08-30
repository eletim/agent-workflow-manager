const code = document.querySelector("#code");
const runButton = document.querySelector("#run");
const stopButton = document.querySelector("#stop");
const statusBadge = document.querySelector("#status");
const stdout = document.querySelector("#stdout");
const stderr = document.querySelector("#stderr");
const outputCopy = document.querySelector("#output-copy");
const exitCode = document.querySelector("#exit-code");
const progress = document.querySelector("#progress");
const progressEmpty = document.querySelector("#progress-empty");
const guideDialog = document.querySelector("#guide-dialog");
const guideOpen = document.querySelector("#guide-open");
const guideClose = document.querySelector("#guide-close");
const guideCopy = document.querySelector("#guide-copy");
const guideContent = document.querySelector("#guide-content");

let timer = null;
let requestToken = null;
let guideText = null;
let outputCopyResetTimer = null;

function render(result) {
  const running = result.state === "running";
  statusBadge.textContent = result.state;
  statusBadge.className = `status ${result.state}`;
  stdout.textContent = result.stdout;
  stderr.textContent = result.stderr;
  exitCode.textContent = `Exit code: ${result.exitCode ?? "—"}`;
  runButton.disabled = running;
  stopButton.disabled = !running;
  renderProgress(result.progress || []);

  if (running) {
    stdout.scrollTop = stdout.scrollHeight;
    stderr.scrollTop = stderr.scrollHeight;
    if (timer === null) timer = window.setInterval(refresh, 500);
  } else if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

function renderProgress(events) {
  const latest = new Map();
  for (const event of events) {
    const key = JSON.stringify([event.name, event.iteration, event.attempt]);
    latest.set(key, event);
  }

  progress.replaceChildren();
  progressEmpty.hidden = latest.size > 0;
  for (const event of latest.values()) {
    const item = document.createElement("li");
    item.className = `progress-item ${event.status}`;

    const marker = document.createElement("span");
    marker.className = "progress-marker";
    marker.textContent = {started: "▶", completed: "✓", failed: "✕"}[event.status];

    const details = document.createElement("div");
    const label = document.createElement("div");
    label.className = "progress-label";
    const number = event.iteration ?? event.attempt;
    label.textContent = `${event.name}${number == null ? "" : ` #${number}`}`;
    details.append(label);

    const noteText = event.error || event.message;
    if (noteText) {
      const note = document.createElement("div");
      note.className = "progress-note";
      note.textContent = noteText;
      details.append(note);
    }
    if (event.workspace || event.tab) {
      const reference = document.createElement("div");
      reference.className = "progress-reference";
      reference.textContent = [event.workspace, event.tab].filter(Boolean).join(" / ");
      details.append(reference);
    }

    item.append(marker, details);
    progress.append(item);
  }
}

async function loadGuide() {
  if (guideText !== null) return guideText;
  const response = await fetch("/python-workflow-guide.md");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  guideText = await response.text();
  guideContent.textContent = guideText;
  guideCopy.disabled = false;
  return guideText;
}

guideOpen.addEventListener("click", async () => {
  guideDialog.showModal();
  try {
    await loadGuide();
  } catch (error) {
    guideContent.textContent = `Could not load Workflow Guide: ${error}`;
  }
});

guideClose.addEventListener("click", () => guideDialog.close());

guideCopy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(await loadGuide());
    guideCopy.textContent = "Copied";
    window.setTimeout(() => { guideCopy.textContent = "Copy"; }, 1200);
  } catch (error) {
    guideContent.textContent = `${guideText || ""}\n\nCopy failed: ${error}`;
  }
});

outputCopy.addEventListener("click", async () => {
  if (outputCopyResetTimer !== null) {
    window.clearTimeout(outputCopyResetTimer);
  }
  try {
    await runnerOutputClipboard.write(
      navigator.clipboard,
      stdout.textContent,
      stderr.textContent,
    );
    outputCopy.textContent = "Copied";
  } catch (error) {
    outputCopy.textContent = "Copy failed";
  }
  outputCopyResetTimer = window.setTimeout(() => {
    outputCopy.textContent = "Copy output";
    outputCopyResetTimer = null;
  }, 1200);
});

async function request(path, options = {}) {
  if (options.method === "POST") {
    options.headers = {
      ...options.headers,
      "X-Python-Runner-Token": requestToken,
    };
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

async function refresh() {
  try {
    render(await request("/api/status"));
  } catch (error) {
    stderr.textContent = String(error);
  }
}

runButton.addEventListener("click", async () => {
  try {
    render(await request("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code: code.value}),
    }));
  } catch (error) {
    stderr.textContent = String(error);
  }
});

stopButton.addEventListener("click", async () => {
  try {
    render(await request("/api/stop", {method: "POST"}));
    await refresh();
  } catch (error) {
    stderr.textContent = String(error);
  }
});

async function initialize() {
  const response = await fetch("/api/token");
  requestToken = (await response.json()).token;
  await refresh();
}

initialize().catch((error) => {
  stderr.textContent = String(error);
  runButton.disabled = true;
});
