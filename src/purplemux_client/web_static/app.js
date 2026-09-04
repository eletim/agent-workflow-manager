const code = document.querySelector("#code");
const runArguments = document.querySelector("#run-arguments");
const promptModeButton = document.querySelector("#prompt-mode");
const workflowModeButton = document.querySelector("#workflow-mode");
const promptFields = document.querySelector("#prompt-fields");
const workflowFields = document.querySelector("#workflow-fields");
const promptAgent = document.querySelector("#prompt-agent");
const promptCwd = document.querySelector("#prompt-cwd");
const promptText = document.querySelector("#prompt-text");
const directoryPickerOpen = document.querySelector("#directory-picker-open");
const directoryPickerDialog = document.querySelector("#directory-picker-dialog");
const directoryPickerClose = document.querySelector("#directory-picker-close");
const directoryPickerParent = document.querySelector("#directory-picker-parent");
const directoryPickerPath = document.querySelector("#directory-picker-path");
const directoryPickerMessage = document.querySelector("#directory-picker-message");
const directoryPickerList = document.querySelector("#directory-picker-list");
const directoryPickerSelect = document.querySelector("#directory-picker-select");
const activeContext = document.querySelector("#active-context");
const runList = document.querySelector("#run-list");
const runsEmpty = document.querySelector("#runs-empty");
const newRunButton = document.querySelector("#new-run");
const runButton = document.querySelector("#run");
const resumeButton = document.querySelector("#resume");
const validateButton = document.querySelector("#validate");
const dryRunButton = document.querySelector("#dry-run");
const stopButton = document.querySelector("#stop");
const cleanupButton = document.querySelector("#cleanup");
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
const resourcesPanel = document.querySelector("#resources-panel");
const resourcesSummary = document.querySelector("#resources-summary");
const executionContextDetails = document.querySelector("#execution-context-details");
const resourcesList = document.querySelector("#resources");
const validationPanel = document.querySelector("#validation-panel");
const validationSuccess = document.querySelector("#validation-success");
const validation = document.querySelector("#validation");
const dryRunPanel = document.querySelector("#dry-run-panel");
const dryRunStatus = document.querySelector("#dry-run-status");
const dryRunEligibility = document.querySelector("#dry-run-eligibility");
const topologyFindings = document.querySelector("#topology-findings");
const nextMutation = document.querySelector("#next-mutation");
const readinessWorkspace = document.querySelector("#readiness-workspace");
const readinessProvider = document.querySelector("#readiness-provider");
const runReadinessButton = document.querySelector("#run-readiness");
const reconcileReadinessButton = document.querySelector("#reconcile-readiness");
const refreshReadinessButton = document.querySelector("#refresh-readiness");
const readinessSummary = document.querySelector("#readiness-summary");
const readinessDetails = document.querySelector("#readiness-details");
const readinessResultProvider = document.querySelector("#readiness-result-provider");
const readinessIdentity = document.querySelector("#readiness-identity");
const readinessTab = document.querySelector("#readiness-tab");
const readinessState = document.querySelector("#readiness-state");
const readinessCleanup = document.querySelector("#readiness-cleanup");
const readinessGuidance = document.querySelector("#readiness-guidance");
const outlinePanel = document.querySelector("#outline-panel");
const outline = document.querySelector("#outline");
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
let currentMode = "workflow";
let rawStdout = "";
let rawStderr = "";
// `activeRunId === null` is the single source of truth for "drafting a new
// run" (fields editable) vs. "viewing an existing run" (fields read-only,
// sourced from that run's authoritative /api/runs/{id} snapshot). `draft`
// retains the new-run args/code independently of whichever run is
// currently being viewed, so switching runs never loses it. `explicitNewRun`
// suppresses the "auto-select the latest run" behavior in refresh() once the
// user has explicitly asked to compose or submit a new run.
let draft = {args: "", code: code.value};
let promptDraft = {
  agent: promptAgent.value,
  cwd: promptCwd.value,
  prompt: promptText.value,
};
let explicitNewRun = false;
let activeRunGeneration = 0;
let refreshRequestGeneration = 0;
let renderedRefreshGeneration = 0;
let validationRequestGeneration = 0;
let eventRefreshActive = false;
let eventRefreshPending = false;
let faviconRunning = false;
let runningFaviconHrefPromise = null;
let directoryPickerCurrentPath = null;
let directoryPickerParentPath = null;
let directoryPickerRequestGeneration = 0;

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
  runArguments.readOnly = !drafting;
  code.readOnly = !drafting;
  promptAgent.disabled = !drafting;
  promptCwd.readOnly = !drafting;
  promptText.readOnly = !drafting;
  directoryPickerOpen.disabled = !drafting;
  runButton.disabled = !drafting;
  validateButton.disabled = !drafting;
  dryRunButton.disabled = !drafting;
  applyModeVisibility();
}

function applyModeVisibility() {
  const promptMode = currentMode === "prompt";
  promptFields.hidden = !promptMode;
  workflowFields.hidden = promptMode;
  validateButton.hidden = promptMode;
  dryRunButton.hidden = promptMode;
  resumeButton.hidden = promptMode;
  cleanupButton.hidden = promptMode;
  guideOpen.hidden = promptMode;
  validationPanel.hidden = promptMode || validationPanel.hidden;
  dryRunPanel.hidden = promptMode || dryRunPanel.hidden;
  outlinePanel.hidden = promptMode || outlinePanel.hidden;
  recoveryPanel.hidden = promptMode || recoveryPanel.hidden;
  resourcesPanel.hidden = promptMode || resourcesPanel.hidden;
  promptModeButton.className = promptMode ? "selected" : "";
  workflowModeButton.className = promptMode ? "" : "selected";
  promptModeButton.setAttribute("aria-pressed", String(promptMode));
  workflowModeButton.setAttribute("aria-pressed", String(!promptMode));
}

function showDraftLabel() {
  activeContext.textContent = `New ${currentMode === "prompt" ? "Prompt" : "Workflow"} run (draft) — not yet submitted`;
}

// Snapshot the fields into the retained draft only when they currently *are*
// the draft (i.e. before something else, like selecting a run, overwrites
// them). Call this right before any transition away from drafting.
function captureDraftIfEditing() {
  if (activeRunId === null) {
    if (currentMode === "prompt") {
      promptDraft = {
        agent: promptAgent.value,
        cwd: promptCwd.value,
        prompt: promptText.value,
      };
    } else {
      draft = {args: runArguments.value, code: code.value};
    }
  }
}

function renderRun(result) {
  currentMode = result.mode === "prompt" ? "prompt" : "workflow";
  const running = result.state === "running";
  statusBadge.textContent = result.state;
  statusBadge.className = `status ${result.state}`;
  rawStdout = result.stdout;
  rawStderr = result.stderr;
  stdout.textContent = runnerLogDisplay.formatOutputEntries(
    result.stdoutEntries,
    rawStdout,
  );
  stderr.textContent = runnerLogDisplay.formatOutputEntries(
    result.stderrEntries,
    rawStderr,
  );
  exitCode.textContent = `Exit code: ${result.exitCode ?? "—"}`;
  stopButton.disabled = activeRunId === null || !running;
  resumeButton.disabled = activeRunId === null || !result.resumable;
  cleanupButton.disabled = activeRunId === null
    || !result.cleanupAvailable
    || ["cleaned", "cleaning"].includes(result.resourceCleanupStatus);
  renderOutline(result.outline || [], result.progress || []);
  renderProgress(result.progress || []);
  renderRecovery(result);
  renderResources(result);
  renderDryRun(result);

  // Only an authoritative snapshot for the run currently being viewed may
  // populate the fields, never a stale response or another run's data.
  if (result.runId != null && result.runId === activeRunId) {
    if (currentMode === "prompt") {
      promptAgent.value = result.prompt?.agent || "codex";
      promptCwd.value = result.prompt?.cwd || result.cwd || "";
      promptText.value = result.prompt?.prompt || "";
    } else {
      runArguments.value = (result.args || []).join("\n");
      code.value = result.code ?? "";
    }
    activeContext.textContent = `Viewing ${currentMode === "prompt" ? "Prompt" : "Workflow"} Run #${result.runId} (read-only)`;
  } else if (activeRunId === null) {
    showDraftLabel();
  }
  applyFieldMode();

  if (running) {
    stdout.scrollTop = stdout.scrollHeight;
    stderr.scrollTop = stderr.scrollHeight;
  }
}

async function enterDraftMode(mode = currentMode) {
  const wasViewingRun = activeRunId !== null;
  const changedMode = mode !== currentMode;
  // A New-run click is an explicit selection even if the fields are already
  // editable. Preserve those live edits while invalidating requests started
  // for the previous selection.
  captureDraftIfEditing();
  // Only the draft/editable fields and the run-scoped controls change here;
  // the output/progress/recovery panels are left showing whatever was last
  // viewed (harmless reference) until a run is selected or started again.
  activeRunGeneration += 1;
  activeRunId = null;
  currentMode = mode;
  explicitNewRun = true;
  if (wasViewingRun || changedMode) {
    if (currentMode === "prompt") {
      promptAgent.value = promptDraft.agent;
      promptCwd.value = promptDraft.cwd;
      promptText.value = promptDraft.prompt;
    } else {
      runArguments.value = draft.args;
      code.value = draft.code;
    }
  }
  showDraftLabel();
  stopButton.disabled = true;
  resumeButton.disabled = true;
  cleanupButton.disabled = true;
  renderOutline([], []);
  applyFieldMode();
  await refresh();
}

function renderResources(result) {
  const resources = result.resources || [];
  resourcesPanel.hidden = result.runId == null;
  const status = result.resourceCleanupStatus || "cleaned";
  resourcesSummary.textContent = resources.length === 0
    ? "No run-owned resources were registered."
    : `${resources.length} registered — ${status.replaceAll("_", " ")}.`;
  const context = result.executionContext;
  executionContextDetails.hidden = context == null;
  executionContextDetails.textContent = context == null
    ? ""
    : `Execution root: ${context.executionRoot} — ${context.baseRef} @ ${context.baseSha}`;
  resourcesList.replaceChildren();
  for (const resource of resources) {
    const item = document.createElement("li");
    const error = resource.cleanupError ? ` — ${resource.cleanupError}` : "";
    item.textContent = `${resource.kind}: ${resource.identity} — ${resource.cleanupState}${error}`;
    resourcesList.append(item);
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
    button.dataset.runId = String(run.runId);
    const mode = run.mode === "prompt" ? "Prompt" : "Workflow";
    const executionRoot = run.mode === "prompt"
      ? run.prompt?.cwd || run.cwd
      : run.executionContext?.executionRoot || "execution context pending";
    button.textContent = `#${run.runId}  ${mode}  ${run.state}  ${executionRoot}`;

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

function renderDryRun(result) {
  const dryRun = result.dryRun;
  const eligibility = result.dryRunIssues || [];
  dryRunPanel.hidden = dryRun == null && eligibility.length === 0;
  dryRunEligibility.replaceChildren();
  for (const issue of eligibility) {
    const item = document.createElement("li");
    item.textContent = issue.message;
    dryRunEligibility.append(item);
  }
  if (!dryRun) {
    dryRunStatus.textContent = result.dryRunEligible
      ? "Eligible — run Dry Run to inspect the reachable frontier."
      : "Ineligible until the contract findings below are resolved.";
    topologyFindings.replaceChildren();
    nextMutation.textContent = "No Dry Run result yet.";
    return;
  }
  dryRunStatus.textContent = {
    frontier: "Stopped truthfully before the first reachable mutation.",
    complete: "Completed without reaching a mutation.",
    failed: "Failed before reaching a safe mutation frontier.",
    ineligible: "Workflow is not eligible for Dry Run.",
  }[dryRun.status] || dryRun.status;
  topologyFindings.replaceChildren();
  for (const finding of dryRun.findings || []) {
    const item = document.createElement("li");
    item.className = finding.status;
    item.textContent = `${finding.category}: ${finding.message}`;
    topologyFindings.append(item);
  }
  nextMutation.textContent = dryRun.nextMutation
    ? `${dryRun.nextMutation.operation} — ${dryRun.nextMutation.target}\n${JSON.stringify(dryRun.nextMutation.preState, null, 2)}`
    : "No reachable mutation.";
}

function renderReadinessProbe(probe) {
  if (!probe) {
    readinessDetails.hidden = true;
    return;
  }
  readinessDetails.hidden = false;
  readinessSummary.className = `readiness-summary ${probe.status}`;
  const messages = {
    succeeded: "Ready, with cleanup confirmed.",
    failed: "The provider did not reach a structured ready state; cleanup was confirmed.",
    unknown: "Outcome requires reconciliation. This is not a successful readiness probe.",
    reconciled: "The unresolved probe is authoritatively absent. The probe block is cleared.",
    pending: "Probe dispatch is pending.",
  };
  readinessSummary.textContent = probe.status === "failed" && probe.tabId == null
    ? "Probe stopped before tab identification; readiness was not observed and cleanup was not attempted."
    : (messages[probe.status] || probe.status);
  readinessResultProvider.textContent = probe.provider;
  readinessIdentity.textContent = `${probe.probeName} (${probe.provider})`;
  readinessTab.textContent = probe.tabId || "Not authoritatively identified";
  readinessState.textContent = probe.readiness;
  readinessCleanup.textContent = probe.cleanup;
  readinessGuidance.textContent = [probe.detail, probe.guidance]
    .filter(Boolean).join(" ") || "None required.";
}

function renderReadiness(snapshot) {
  const selected = readinessWorkspace.value;
  readinessWorkspace.replaceChildren();
  for (const workspace of snapshot.workspaces || []) {
    const option = document.createElement("option");
    option.value = workspace.id;
    option.textContent = `${workspace.name} (${workspace.id})`;
    readinessWorkspace.append(option);
  }
  const ids = (snapshot.workspaces || []).map((workspace) => workspace.id);
  readinessWorkspace.value = ids.includes(selected) ? selected : (ids[0] || "");
  const unavailable = ids.length === 0;
  const reconciliationRequired = ["pending", "unknown"].includes(snapshot.probe?.status);
  readinessWorkspace.disabled = unavailable || snapshot.running;
  readinessProvider.disabled = unavailable || snapshot.running;
  runReadinessButton.disabled = unavailable || snapshot.running || reconciliationRequired;
  reconcileReadinessButton.hidden = !reconciliationRequired;
  reconcileReadinessButton.disabled = snapshot.running;
  if (unavailable) {
    readinessSummary.className = "readiness-summary failed";
    readinessSummary.textContent = "No existing PurpleMux workspace is available. Create or select one outside this probe.";
    readinessDetails.hidden = true;
  } else if (!snapshot.probe) {
    readinessSummary.className = "readiness-summary";
    readinessSummary.textContent = "Not run. Static Validation and Dry Run never invoke this mutating probe.";
  }
  renderReadinessProbe(snapshot.probe);
}

async function refreshReadiness() {
  try {
    renderReadiness(await request("/api/readiness"));
  } catch (error) {
    readinessSummary.className = "readiness-summary failed";
    readinessSummary.textContent = String(error);
    runReadinessButton.disabled = true;
  }
}

function renderOutline(labels, events) {
  const states = new Map(labels.map((label) => [label, "pending"]));
  for (const event of events) {
    if (!states.has(event.name)) continue;
    states.set(event.name, {
      started: "running",
      completed: "completed",
      failed: "failed",
    }[event.status]);
  }

  outline.replaceChildren();
  outlinePanel.hidden = labels.length === 0;
  for (const label of labels) {
    const state = states.get(label);
    const item = document.createElement("li");
    item.className = `outline-item ${state}`;

    const marker = document.createElement("span");
    marker.className = "outline-marker";
    marker.setAttribute("aria-hidden", "true");
    marker.textContent = {
      pending: "○",
      running: "▶",
      completed: "✓",
      failed: "✕",
    }[state];

    const text = document.createElement("span");
    text.textContent = label;
    item.append(marker, text);
    outline.append(item);
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
    rawStdout,
    rawStderr,
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

async function browseDirectory(path) {
  const requestGeneration = ++directoryPickerRequestGeneration;
  directoryPickerMessage.textContent = "Loading…";
  directoryPickerParent.disabled = true;
  directoryPickerSelect.disabled = true;
  try {
    const listing = await request("/api/directories", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path}),
    });
    if (requestGeneration !== directoryPickerRequestGeneration) return;
    directoryPickerCurrentPath = listing.path;
    directoryPickerParentPath = listing.parent;
    directoryPickerPath.textContent = listing.path;
    directoryPickerPath.setAttribute("title", listing.path);
    directoryPickerMessage.textContent = "";
    directoryPickerParent.disabled = listing.parent === null;
    directoryPickerSelect.disabled = false;
    directoryPickerList.replaceChildren();
    if (listing.directories.length === 0) {
      const empty = document.createElement("p");
      empty.className = "directory-picker-empty";
      empty.textContent = "No subdirectories.";
      directoryPickerList.append(empty);
    }
    for (const directory of listing.directories) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `📁 ${directory.name}`;
      button.setAttribute("title", directory.path);
      button.addEventListener("click", () => {
        void browseDirectory(directory.path);
      });
      directoryPickerList.append(button);
    }
  } catch (error) {
    if (requestGeneration !== directoryPickerRequestGeneration) return;
    directoryPickerMessage.textContent = String(error);
    directoryPickerSelect.disabled = directoryPickerCurrentPath === null;
    directoryPickerParent.disabled = directoryPickerParentPath === null;
  }
}

directoryPickerOpen.addEventListener("click", () => {
  if (activeRunId !== null) return;
  directoryPickerCurrentPath = null;
  directoryPickerParentPath = null;
  directoryPickerPath.textContent = "";
  directoryPickerList.replaceChildren();
  directoryPickerDialog.showModal();
  void browseDirectory(promptCwd.value || "~");
});

directoryPickerClose.addEventListener("click", () => {
  directoryPickerRequestGeneration += 1;
  directoryPickerDialog.close();
});

directoryPickerParent.addEventListener("click", () => {
  if (directoryPickerParentPath !== null) {
    void browseDirectory(directoryPickerParentPath);
  }
});

directoryPickerSelect.addEventListener("click", () => {
  if (directoryPickerCurrentPath === null || activeRunId !== null) return;
  promptCwd.value = directoryPickerCurrentPath;
  promptDraft.cwd = directoryPickerCurrentPath;
  directoryPickerRequestGeneration += 1;
  directoryPickerDialog.close();
  promptCwd.focus();
});

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
    args: runArguments.value === "" ? [] : runArguments.value.split("\n"),
  };
}

runButton.addEventListener("click", async () => {
  if (activeRunId !== null) return; // must explicitly start a New run first
  captureDraftIfEditing();
  const submittedMode = currentMode;
  const selectionGeneration = ++activeRunGeneration;
  explicitNewRun = true;
  const validationGeneration = ++validationRequestGeneration;
  try {
    const requestPath = submittedMode === "prompt" ? "/api/prompt" : "/api/run";
    const payload = submittedMode === "prompt"
      ? {
        agent: promptAgent.value,
        cwd: promptCwd.value,
        prompt: promptText.value,
      }
      : {code: code.value, ...executionContextPayload()};
    const result = await request(requestPath, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    if (selectionGeneration === activeRunGeneration) {
      // The fields remain editable while the request is pending. Retain any
      // changes made since submission before replacing them with the run's
      // authoritative snapshot.
      captureDraftIfEditing();
      activeRunId = result.runId;
      activeRunGeneration += 1;
      explicitNewRun = false;
      if (
        submittedMode === "workflow"
        && validationGeneration === validationRequestGeneration
      ) {
        renderValidation(result.validation || []);
      }
      renderRun(result);
    }
    await refresh();
  } catch (error) {
    if (Array.isArray(error.result?.validation)) {
      if (
        validationGeneration === validationRequestGeneration
        && selectionGeneration === activeRunGeneration
        && activeRunId === null
      ) {
        renderValidation(error.result.validation);
        renderOutline(error.result.outline || [], []);
      }
    } else if (selectionGeneration === activeRunGeneration) {
      stderr.textContent = String(error);
    }
  }
});

newRunButton.addEventListener("click", async () => {
  await enterDraftMode();
});

promptModeButton.addEventListener("click", async () => {
  await enterDraftMode("prompt");
});

workflowModeButton.addEventListener("click", async () => {
  await enterDraftMode("workflow");
});

validateButton.addEventListener("click", async () => {
  if (activeRunId !== null) return; // validate the draft, never a viewed run's snapshot
  const requestGeneration = ++validationRequestGeneration;
  const selectionGeneration = activeRunGeneration;
  try {
    const result = await request("/api/validate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code: code.value, ...executionContextPayload()}),
    });
    if (
      requestGeneration === validationRequestGeneration
      && selectionGeneration === activeRunGeneration
      && activeRunId === null
    ) {
      renderValidation(result.validation || []);
      renderOutline(result.outline || [], []);
    }
  } catch (error) {
    if (
      requestGeneration === validationRequestGeneration
      && selectionGeneration === activeRunGeneration
      && activeRunId === null
      && Array.isArray(error.result?.validation)
    ) {
      renderValidation(error.result.validation);
      renderOutline(error.result.outline || [], []);
    } else if (
      requestGeneration === validationRequestGeneration
      && selectionGeneration === activeRunGeneration
      && activeRunId === null
    ) {
      stderr.textContent = String(error);
    }
  }
});

dryRunButton.addEventListener("click", async () => {
  if (activeRunId !== null) return;
  const selectionGeneration = activeRunGeneration;
  try {
    const result = await request("/api/dry-run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code: code.value, ...executionContextPayload()}),
    });
    if (selectionGeneration === activeRunGeneration && activeRunId === null) {
      renderValidation(result.validation || []);
      renderOutline(result.outline || [], []);
      renderDryRun(result);
    }
  } catch (error) {
    if (selectionGeneration === activeRunGeneration && activeRunId === null) {
      if (error.result) {
        renderValidation(error.result.validation || []);
        renderDryRun(error.result);
      } else {
        stderr.textContent = String(error);
      }
    }
  }
});

refreshReadinessButton.addEventListener("click", refreshReadiness);

runReadinessButton.addEventListener("click", async () => {
  if (!readinessWorkspace.value) return;
  runReadinessButton.disabled = true;
  refreshReadinessButton.disabled = true;
  readinessSummary.className = "readiness-summary";
  readinessSummary.textContent = "Creating exactly one probe tab…";
  try {
    const result = await request("/api/readiness/probe", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        workspaceId: readinessWorkspace.value,
        provider: readinessProvider.value,
      }),
    });
    renderReadinessProbe(result.probe);
  } catch (error) {
    if (error.result?.probe) renderReadinessProbe(error.result.probe);
    else {
      readinessSummary.className = "readiness-summary failed";
      readinessSummary.textContent = String(error);
    }
  } finally {
    refreshReadinessButton.disabled = false;
    await refreshReadiness();
  }
});

reconcileReadinessButton.addEventListener("click", async () => {
  reconcileReadinessButton.disabled = true;
  readinessSummary.className = "readiness-summary";
  readinessSummary.textContent = "Authoritatively inspecting the unresolved probe identity…";
  try {
    const result = await request("/api/readiness/reconcile", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}",
    });
    renderReadinessProbe(result.probe);
  } catch (error) {
    readinessSummary.className = "readiness-summary failed";
    readinessSummary.textContent = String(error);
  } finally {
    await refreshReadiness();
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

cleanupButton.addEventListener("click", async () => {
  if (activeRunId === null) return;
  const targetRunId = activeRunId;
  const selectionGeneration = ++activeRunGeneration;
  cleanupButton.disabled = true;
  try {
    const result = await request(`/api/runs/${targetRunId}/cleanup`, {method: "POST"});
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
  await refreshReadiness();
}

initialize().catch((error) => {
  stderr.textContent = String(error);
  runButton.disabled = true;
});
