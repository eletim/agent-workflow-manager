const code = document.querySelector("#code");
const runButton = document.querySelector("#run");
const stopButton = document.querySelector("#stop");
const statusBadge = document.querySelector("#status");
const stdout = document.querySelector("#stdout");
const stderr = document.querySelector("#stderr");
const exitCode = document.querySelector("#exit-code");
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

function render(result) {
  const running = result.state === "running";
  statusBadge.textContent = result.state;
  statusBadge.className = `status ${result.state}`;
  stdout.textContent = result.stdout;
  stderr.textContent = result.stderr;
  exitCode.textContent = `Exit code: ${result.exitCode ?? "—"}`;
  runButton.disabled = running;
  stopButton.disabled = !running;

  if (running) {
    stdout.scrollTop = stdout.scrollHeight;
    stderr.scrollTop = stderr.scrollHeight;
    if (timer === null) timer = window.setInterval(refresh, 500);
  } else if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

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
