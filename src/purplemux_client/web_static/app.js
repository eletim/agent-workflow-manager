const code = document.querySelector("#code");
const workingDirectory = document.querySelector("#working-directory");
const runArguments = document.querySelector("#run-arguments");
const activeContext = document.querySelector("#active-context");
const runList = document.querySelector("#run-list");
const runsEmpty = document.querySelector("#runs-empty");
const newRunButton = document.querySelector("#new-run");
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
const validationSuccess = document.querySelector("#validation-success");
const validation = document.querySelector("#validation");
const guideDialog = document.querySelector("#guide-dialog");
const guideOpen = document.querySelector("#guide-open");
const guideClose = document.querySelector("#guide-close");
const guideCopy = document.querySelector("#guide-copy");
const guideContent = document.querySelector("#guide-content");
const manualCopyDialog = document.querySelector("#manual-copy-dialog");
const manualCopyContent = document.querySelector("#manual-copy-content");
const manualCopyClose = document.querySelector("#manual-copy-close");
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
const favicon = document.querySelector("#favicon");

let requestToken = null;
let eventSource = null;
let guideText = null;
let guideCopyResetTimer = null;
let outputCopyResetTimer = null;
let activeRunId = null;
// `activeRunId === null` is the single source of truth for "drafting a new
// run" (fields editable) vs. "viewing an existing run" (fields read-only,
// sourced from that run's authoritative /api/runs/{id} snapshot). `draft`
// retains the new-run cwd/args/code independently of whichever run is
// currently being viewed, so switching runs never loses it. `explicitNewRun`
// only suppresses the "auto-select the latest run" behavior in refresh() once
// the user has explicitly asked to compose a new run.
let draft = {cwd: "", args: "", code: code.value};
let explicitNewRun = false;
let activeRunGeneration = 0;
let refreshRequestGeneration = 0;
let renderedRefreshGeneration = 0;
let validationRequestGeneration = 0;
let eventRefreshActive = false;
let eventRefreshPending = false;
let faviconRunning = false;
let runningFaviconHrefPromise = null;

function runningFaviconHref() {
  if (runningFaviconHrefPromise === null) {
    runningFaviconHrefPromise = fetch(favicon.getAttribute("href"))
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.text();
      })
      .then((source) => {
        const badge = '<circle id="running-badge" cx="25" cy="25" r="5" fill="#ef4444" stroke="#fff" stroke-width="2"/>';
        return `data:image/svg+xml,${encodeURIComponent(source.replace("</svg>", `${badge}</svg>`))}`;
      });
  }
  return runningFaviconHrefPromise;
}

function renderFavicon(runs) {
  faviconRunning = runs.some((run) => run.state === "running");
  if (!faviconRunning) {
    favicon.setAttribute("href", "/favicon.svg");
    return;
  }
  void runningFaviconHref().then((href) => {
    if (faviconRunning) favicon.setAttribute("href", href);
  }).catch(() => {
    // The base icon remains usable if a browser cannot generate the badge.
  });
}

// Fields are editable draft while no run is selected, and a read-only view of
// that run's immutable snapshot once one is. Keep this in sync with
// `activeRunId` after every render, since that's the single source of truth
// for which mode is active.
function applyFieldMode() {
  const drafting = activeRunId === null;
  workingDirectory.readOnly = !drafting;
  runArguments.readOnly = !drafting;
  code.readOnly = !drafting;
  runButton.disabled = !drafting;
  validateButton.disabled = !drafting;
}

function showDraftLabel() {
  activeContext.textContent = "New run (draft) — not yet submitted";
}

// Snapshot the fields into the retained draft only when they currently *are*
// the draft (i.e. before something else, like selecting a run, overwrites
// them). Call this right before any transition away from drafting.
function captureDraftIfEditing() {
  if (activeRunId === null) {
    draft = {cwd: workingDirectory.value, args: runArguments.value, code: code.value};
  }
}

function renderRun(result) {
  const running = result.state === "running";
  statusBadge.textContent = result.state;
  statusBadge.className = `status ${result.state}`;
  stdout.textContent = result.stdout;
  stderr.textContent = result.stderr;
  exitCode.textContent = `Exit code: ${result.exitCode ?? "—"}`;
  stopButton.disabled = activeRunId === null || !running;
  resumeButton.disabled = activeRunId === null || !result.resumable;
  renderProgress(result.progress || []);
  renderRecovery(result);

  // Only an authoritative snapshot for the run currently being viewed may
  // populate the fields, never a stale response or another run's data.
  if (result.runId != null && result.runId === activeRunId) {
    workingDirectory.value = result.cwd ?? "";
    runArguments.value = (result.args || []).join("\n");
    code.value = result.code ?? "";
    activeContext.textContent = `Viewing Run #${result.runId} (read-only)`;
  } else if (activeRunId === null) {
    showDraftLabel();
  }
  applyFieldMode();

  if (running) {
    stdout.scrollTop = stdout.scrollHeight;
    stderr.scrollTop = stderr.scrollHeight;
  }
}

async function enterDraftMode() {
  const wasViewingRun = activeRunId !== null;
  // A New-run click is an explicit selection even if the fields are already
  // editable. Preserve those live edits while invalidating requests started
  // for the previous selection.
  captureDraftIfEditing();
  // Only the draft/editable fields and the run-scoped controls change here;
  // the output/progress/recovery panels are left showing whatever was last
  // viewed (harmless reference) until a run is selected or started again.
  activeRunGeneration += 1;
  activeRunId = null;
  explicitNewRun = true;
  if (wasViewingRun) {
    workingDirectory.value = draft.cwd;
    runArguments.value = draft.args;
    code.value = draft.code;
  }
  showDraftLabel();
  stopButton.disabled = true;
  resumeButton.disabled = true;
  applyFieldMode();
  await refresh();
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
    button.dataset.runId = String(run.runId);
    button.textContent = `#${run.runId}  ${run.state}  ${run.cwd}`;

    const marker = document.createElement("span");
    marker.className = "run-state-marker";
    marker.setAttribute("aria-hidden", "true");
    button.prepend(marker);
    button.addEventListener("click", async () => {
      captureDraftIfEditing();
      activeRunId = run.runId;
      activeRunGeneration += 1;
      explicitNewRun = false;
      await refresh();
    });
    runList.append(button);
  }
}

function renderValidation(issues) {
  validation.replaceChildren();
  const valid = issues.length === 0;
  validationPanel.hidden = false;
  validationPanel.className = `panel validation-panel ${valid ? "valid" : "invalid"}`;
  validationSuccess.hidden = !valid;
  validation.hidden = valid;
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

function showManualCopy(text) {
  manualCopyContent.value = text;
  manualCopyDialog.showModal();
  manualCopyContent.focus();
  manualCopyContent.select();
  manualCopyContent.setSelectionRange(0, text.length);
}

manualCopyClose.addEventListener("click", () => manualCopyDialog.close());

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
  if (guideCopyResetTimer !== null) {
    window.clearTimeout(guideCopyResetTimer);
  }
  try {
    const text = await loadGuide();
    await runnerOutputClipboard.writeText(
      text,
      navigator.clipboard,
      document,
    );
    guideCopy.textContent = "Copied";
  } catch (error) {
    guideCopy.textContent = "Copy manually";
    showManualCopy(guideText || guideContent.textContent);
  }
  guideCopyResetTimer = window.setTimeout(() => {
    guideCopy.textContent = "Copy";
    guideCopyResetTimer = null;
  }, 1200);
});

outputCopy.addEventListener("click", async () => {
  if (outputCopyResetTimer !== null) {
    window.clearTimeout(outputCopyResetTimer);
  }
  const text = runnerOutputClipboard.formatOutput(
    stdout.textContent,
    stderr.textContent,
  );
  try {
    await runnerOutputClipboard.writeText(
      text,
      navigator.clipboard,
      document,
    );
    outputCopy.textContent = "Copied";
  } catch (error) {
    outputCopy.textContent = "Copy manually";
    showManualCopy(text);
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
  const requestGeneration = ++refreshRequestGeneration;
  let selectionGeneration = activeRunGeneration;
  try {
    const {runs} = await request("/api/runs");
    if (selectionGeneration !== activeRunGeneration) return;
    if (activeRunId === null && !explicitNewRun && runs.length > 0) {
      // A run just appeared (e.g. discovered via SSE) while the fields held
      // in-progress draft edits nobody submitted yet; retain them before
      // auto-selecting, exactly as an explicit run-list click would.
      captureDraftIfEditing();
      activeRunId = runs[runs.length - 1].runId;
      activeRunGeneration += 1;
      selectionGeneration = activeRunGeneration;
    }
    const targetRunId = activeRunId;
    const selected = runs.find((run) => run.runId === targetRunId);
    const result = selected
      ? await request(`/api/runs/${targetRunId}`)
      : null;
    if (
      selectionGeneration !== activeRunGeneration
      || targetRunId !== activeRunId
      || requestGeneration <= renderedRefreshGeneration
    ) return;
    if (result) renderRun(result);
    renderRunList(runs);
    renderFavicon(runs);
    renderedRefreshGeneration = requestGeneration;
  } catch (error) {
    if (
      selectionGeneration === activeRunGeneration
      && requestGeneration > renderedRefreshGeneration
    ) stderr.textContent = String(error);
  }
}

function connectEvents() {
  eventSource = new window.EventSource("/api/events");
  eventSource.addEventListener("runner-change", () => {
    void scheduleEventRefresh();
  });
  eventSource.addEventListener("open", () => {
    // EventSource reconnects automatically. Reconcile because notifications may
    // have been missed while the connection was unavailable.
    void scheduleEventRefresh();
  });
}

async function scheduleEventRefresh() {
  if (eventRefreshActive) {
    eventRefreshPending = true;
    return;
  }
  eventRefreshActive = true;
  try {
    do {
      eventRefreshPending = false;
      await refresh();
    } while (eventRefreshPending);
  } finally {
    eventRefreshActive = false;
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
  if (activeRunId !== null) return; // must explicitly start a New run first
  captureDraftIfEditing();
  const selectionGeneration = ++activeRunGeneration;
  const validationGeneration = ++validationRequestGeneration;
  try {
    const result = await request("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code: code.value, ...executionContextPayload()}),
    });
    if (selectionGeneration === activeRunGeneration) {
      // The fields remain editable while the request is pending. Retain any
      // changes made since submission before replacing them with the run's
      // authoritative snapshot.
      captureDraftIfEditing();
      activeRunId = result.runId;
      activeRunGeneration += 1;
      explicitNewRun = false;
      if (validationGeneration === validationRequestGeneration) {
        renderValidation(result.validation || []);
      }
      renderRun(result);
    }
    await refresh();
  } catch (error) {
    if (Array.isArray(error.result?.validation)) {
      if (validationGeneration === validationRequestGeneration) {
        renderValidation(error.result.validation);
      }
    } else if (selectionGeneration === activeRunGeneration) {
      stderr.textContent = String(error);
    }
  }
});

newRunButton.addEventListener("click", async () => {
  await enterDraftMode();
});

validateButton.addEventListener("click", async () => {
  if (activeRunId !== null) return; // validate the draft, never a viewed run's snapshot
  const requestGeneration = ++validationRequestGeneration;
  try {
    const result = await request("/api/validate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code: code.value, ...executionContextPayload()}),
    });
    if (requestGeneration === validationRequestGeneration) {
      renderValidation(result.validation || []);
    }
  } catch (error) {
    if (
      requestGeneration === validationRequestGeneration
      && Array.isArray(error.result?.validation)
    ) {
      renderValidation(error.result.validation);
    } else if (requestGeneration === validationRequestGeneration) {
      stderr.textContent = String(error);
    }
  }
});

stopButton.addEventListener("click", async () => {
  if (activeRunId === null) return;
  const targetRunId = activeRunId;
  const selectionGeneration = ++activeRunGeneration;
  try {
    const result = await request(`/api/runs/${targetRunId}/stop`, {method: "POST"});
    if (
      targetRunId === activeRunId
      && selectionGeneration === activeRunGeneration
    ) {
      activeRunGeneration += 1;
      renderRun(result);
    }
    await refresh();
  } catch (error) {
    if (
      targetRunId === activeRunId
      && selectionGeneration === activeRunGeneration
    ) stderr.textContent = String(error);
  }
});

resumeButton.addEventListener("click", async () => {
  if (activeRunId === null) return;
  const targetRunId = activeRunId;
  const selectionGeneration = ++activeRunGeneration;
  const validationGeneration = ++validationRequestGeneration;
  resumeButton.disabled = true;
  try {
    const result = await request(`/api/runs/${targetRunId}/resume`, {method: "POST"});
    if (
      targetRunId === activeRunId
      && selectionGeneration === activeRunGeneration
    ) {
      activeRunGeneration += 1;
      renderRun(result);
    }
    await refresh();
  } catch (error) {
    if (Array.isArray(error.result?.validation)) {
      if (validationGeneration === validationRequestGeneration) {
        renderValidation(error.result.validation);
      }
    } else if (
      targetRunId === activeRunId
      && selectionGeneration === activeRunGeneration
    ) stderr.textContent = String(error);
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
  if (initialStatus.state === "validation_failed") {
    renderValidation(initialStatus.validation || []);
  }
  renderRun(initialStatus);
  await refresh();
  connectEvents();
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
