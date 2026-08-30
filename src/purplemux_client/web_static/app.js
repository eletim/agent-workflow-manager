const code = document.querySelector("#code");
const runButton = document.querySelector("#run");
const stopButton = document.querySelector("#stop");
const statusBadge = document.querySelector("#status");
const stdout = document.querySelector("#stdout");
const stderr = document.querySelector("#stderr");
const exitCode = document.querySelector("#exit-code");

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
