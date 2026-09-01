const code = document.querySelector("#code");
const workingDirectory = document.querySelector("#working-directory");
const runArguments = document.querySelector("#run-arguments");
const activeContext = document.querySelector("#active-context");
const runList = document.querySelector("#run-list");
const runsEmpty = document.querySelector("#runs-empty");
const runButton = document.querySelector("#run");
const resumeButton = document.querySelector("#resume");
const validateButton = document.querySelector("#validate");
const stopButton = document.querySelector("#stop");
const statusBadge = document.querySelector("#status");
const stdout = document.querySelector("#stdout");
const stderr = document.querySelector("#stderr");
const outputCopy = document.querySelector("#output-copy");
const exitCode = document.querySelector("#exit-code");
const progress = document.querySelector("#progress");
const progressEmpty = document.querySelector("#progress-empty");
const recoveryPanel = document.querySelector("#recovery-panel");
const recoverySummary = document.querySelector("#recovery-summary");
const attemptHistory = document.querySelector("#attempt-history");
const validationPanel = document.querySelector("#validation-panel");
const validation = document.querySelector("#validation");
const guideDialog = document.querySelector("#guide-dialog");
const guideOpen = document.querySelector("#guide-open");
const guideClose = document.querySelector("#guide-close");
const guideCopy = document.querySelector("#guide-copy");
const guideContent = document.querySelector("#guide-content");
const settingsForm = document.querySelector("#notification-settings");
const notificationsEnabled = document.querySelector("#notifications-enabled");
const notifySuccess = document.querySelector("#notify-success");
const notifyFailure = document.querySelector("#notify-failure");
const notifyStopped = document.querySelector("#notify-stopped");
const notifyServer = document.querySelector("#notify-server");
const notifyTopic = document.querySelector("#notify-topic");
const replacementToken = document.querySelector("#replacement-token");
const credentialStatus = document.querySelector("#credential-status");
const settingsMessage = document.querySelector("#settings-message");
const saveSettingsButton = document.querySelector("#save-settings");
const testNotificationButton = document.querySelector("#test-notification");

let timer = null;
let requestToken = null;
let guideText = null;
let outputCopyResetTimer = null;
let activeRunId = null;

function render(result) {
  const running = result.state === "running";
  statusBadge.textContent = result.state;
  statusBadge.className = `status ${result.state}`;
  stdout.textContent = result.stdout;
  stderr.textContent = result.stderr;
  exitCode.textContent = `Exit code: ${result.exitCode ?? "—"}`;
  runButton.disabled = false;
  validateButton.disabled = false;
  stopButton.disabled = activeRunId === null || !running;
  resumeButton.disabled = activeRunId === null || !result.resumable;
  renderProgress(result.progress || []);
  renderValidation(result.validation || []);
  renderRecovery(result);
  const renderedArgs = (result.args || []).map((argument) => JSON.stringify(argument)).join(" ");
  const label = result.runId == null ? "Configured run" : `Run #${result.runId}`;
  activeContext.textContent = `${label}: ${result.cwd}${renderedArgs ? ` ${renderedArgs}` : ""}`;

  if (running) {
    stdout.scrollTop = stdout.scrollHeight;
    stderr.scrollTop = stderr.scrollHeight;
  }
}

function renderRecovery(result) {
  const attempts = result.attempts || [];
  recoveryPanel.hidden = !["failed", "suspended"].includes(result.state)
    && result.checkpoint == null && attempts.length < 2;
  if (result.checkpoint) {
    recoverySummary.textContent = `Safe checkpoint: ${result.checkpoint.name}. Manual fixes are preserved; the Python workflow decides how continuation validates and uses this checkpoint.`;
  } else if (result.state === "failed") {
    recoverySummary.textContent = "No safe checkpoint was published. This run cannot be resumed without risking replay of completed side effects.";
  } else {
    recoverySummary.textContent = result.suspensionReason || "";
  }
  if (result.suspensionReason) {
    recoverySummary.textContent += ` Suspended: ${result.suspensionReason}`;
  }
  attemptHistory.replaceChildren();
  for (const attempt of attempts) {
    const item = document.createElement("li");
    item.textContent = `Attempt ${attempt.number}: ${attempt.state} (exit ${attempt.exitCode})${attempt.resumedFrom ? `, resumed from ${attempt.resumedFrom}` : ""}`;
    attemptHistory.append(item);
  }
}

function renderRunList(runs) {
  runList.replaceChildren();
  runsEmpty.hidden = runs.length > 0;
  for (const run of [...runs].reverse()) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `run-item ${run.runId === activeRunId ? "selected" : ""}`;
    button.dataset.state = run.state;
    button.textContent = `#${run.runId}  ${run.state}  ${run.cwd}`;
    button.addEventListener("click", async () => {
      activeRunId = run.runId;
      await refresh();
    });
    runList.append(button);
  }
}

function updatePolling(runs) {
  if (runs.some((run) => run.state === "running") && timer === null) {
    timer = window.setInterval(refresh, 500);
  } else if (!runs.some((run) => run.state === "running") && timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

function renderValidation(issues) {
  validation.replaceChildren();
  validationPanel.hidden = issues.length === 0;
  for (const issue of issues) {
    const item = document.createElement("li");
    const location = issue.line == null
      ? ""
      : `Line ${issue.line}${issue.column == null ? "" : `:${issue.column}`}: `;
    item.textContent = `${location}${issue.message}`;
    validation.append(item);
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
  if (!response.ok) {
    const error = new Error(result.error || `HTTP ${response.status}`);
    error.result = result;
    throw error;
  }
  return result;
}

async function refresh() {
  try {
    const {runs} = await request("/api/runs");
    if (activeRunId === null && runs.length > 0) {
      activeRunId = runs[runs.length - 1].runId;
    }
    const selected = runs.find((run) => run.runId === activeRunId);
    if (selected) render(await request(`/api/runs/${activeRunId}`));
    renderRunList(runs);
    updatePolling(runs);
  } catch (error) {
    stderr.textContent = String(error);
  }
}

function renderSettings(settings) {
  notificationsEnabled.checked = settings.enabled;
  notifySuccess.checked = settings.onSuccess;
  notifyFailure.checked = settings.onFailure;
  notifyStopped.checked = settings.onStopped;
  notifyServer.value = settings.server;
  notifyTopic.value = settings.topic;
  const configured = settings.credentialStatus === "configured";
  credentialStatus.textContent = `Credentials: ${configured ? "Configured" : "Missing"}`;
  credentialStatus.className = `credential ${configured ? "configured" : "missing"}`;
}

function showSettingsMessage(message, isError = false) {
  settingsMessage.textContent = message;
  settingsMessage.className = isError ? "error" : "success-message";
}

function settingsPayload() {
  const payload = {
    enabled: notificationsEnabled.checked,
    onSuccess: notifySuccess.checked,
    onFailure: notifyFailure.checked,
    onStopped: notifyStopped.checked,
    server: notifyServer.value,
    topic: notifyTopic.value,
  };
  if (replacementToken.value) payload.replacementToken = replacementToken.value;
  return payload;
}

function executionContextPayload() {
  return {
    cwd: workingDirectory.value.trim() || null,
    args: runArguments.value === "" ? [] : runArguments.value.split("\n"),
  };
}

runButton.addEventListener("click", async () => {
  try {
    const result = await request("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code: code.value, ...executionContextPayload()}),
    });
    activeRunId = result.runId;
    render(result);
    await refresh();
  } catch (error) {
    if (error.result) render(error.result);
    stderr.textContent = String(error);
  }
});

validateButton.addEventListener("click", async () => {
  try {
    render(await request("/api/validate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code: code.value, ...executionContextPayload()}),
    }));
  } catch (error) {
    if (error.result) render(error.result);
    else stderr.textContent = String(error);
  }
});

stopButton.addEventListener("click", async () => {
  if (activeRunId === null) return;
  try {
    render(await request(`/api/runs/${activeRunId}/stop`, {method: "POST"}));
    await refresh();
  } catch (error) {
    stderr.textContent = String(error);
  }
});

resumeButton.addEventListener("click", async () => {
  if (activeRunId === null) return;
  resumeButton.disabled = true;
  try {
    render(await request(`/api/runs/${activeRunId}/resume`, {method: "POST"}));
    await refresh();
  } catch (error) {
    stderr.textContent = String(error);
    await refresh();
  }
});

settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveSettingsButton.disabled = true;
  showSettingsMessage("Saving…");
  try {
    const settings = await request("/api/settings/notifications", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(settingsPayload()),
    });
    replacementToken.value = "";
    renderSettings(settings);
    showSettingsMessage("Settings saved. Changes apply immediately.");
  } catch (error) {
    replacementToken.value = "";
    showSettingsMessage(String(error), true);
  } finally {
    saveSettingsButton.disabled = false;
  }
});

testNotificationButton.addEventListener("click", async () => {
  testNotificationButton.disabled = true;
  showSettingsMessage("Sending…");
  try {
    const result = await request("/api/settings/notifications/test", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}",
    });
    showSettingsMessage(result.message);
  } catch (error) {
    showSettingsMessage(String(error), true);
  } finally {
    testNotificationButton.disabled = false;
  }
});

async function initialize() {
  const response = await fetch("/api/token");
  requestToken = (await response.json()).token;
  const initialStatus = await request("/api/status");
  if (!workingDirectory.value) workingDirectory.value = initialStatus.cwd;
  render(initialStatus);
  await refresh();
  try {
    renderSettings(await request("/api/settings/notifications"));
  } catch (error) {
    showSettingsMessage(String(error), true);
  }
}

initialize().catch((error) => {
  stderr.textContent = String(error);
  runButton.disabled = true;
});
