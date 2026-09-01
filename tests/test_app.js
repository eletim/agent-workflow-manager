"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs.readFileSync(path.join(
  __dirname,
  "..",
  "src",
  "purplemux_client",
  "web_static",
  "app.js",
), "utf8");

class Element {
  constructor() {
    this.attributes = {};
    this.checked = false;
    this.children = [];
    this.className = "";
    this.dataset = {};
    this.disabled = false;
    this.hidden = false;
    this.listeners = new Map();
    this.scrollHeight = 0;
    this.scrollTop = 0;
    this.textContent = "";
    this.value = "";
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  async dispatch(type) {
    const event = {preventDefault() {}};
    for (const listener of this.listeners.get(type) || []) {
      await listener(event);
    }
  }

  showModal() {}

  close() {}
}

function snapshot({
  runId,
  state,
  stdout,
  resumable = false,
  checkpoint = null,
  attempts = [],
}) {
  return {
    args: [],
    attempts,
    checkpoint,
    cwd: `/work/run-${runId}`,
    exitCode: state === "running" ? null : 1,
    progress: [{name: `step-${runId}`, status: "completed"}],
    resumable,
    runId,
    state,
    stderr: `stderr-${runId}`,
    stdout,
    suspensionReason: null,
    validation: [],
  };
}

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return body; },
    async text() { return String(body); },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((promiseResolve) => { resolve = promiseResolve; });
  return {promise, resolve};
}

async function loadApp({runs, details, validation, fetchOverride = null}) {
  const ids = [
    "code", "working-directory", "run-arguments", "active-context", "run-list",
    "runs-empty", "run", "resume", "validate", "stop", "status", "stdout",
    "stderr", "output-copy", "exit-code", "progress", "progress-empty",
    "recovery-panel", "recovery-summary", "attempt-history", "validation-panel",
    "validation", "guide-dialog", "guide-open", "guide-close", "guide-copy",
    "guide-content", "manual-copy-dialog", "manual-copy-content",
    "manual-copy-close", "notification-settings", "notifications-enabled",
    "notify-success", "notify-failure", "notify-stopped", "notify-server",
    "notify-topic", "replacement-token", "credential-status", "settings-message",
    "save-settings", "test-notification",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element()]));
  const calls = [];
  const document = {
    body: new Element(),
    createElement() { return new Element(); },
    execCommand() { return true; },
    querySelector(selector) { return elements[selector.slice(1)]; },
  };
  const settings = {
    credentialStatus: "missing",
    enabled: false,
    onFailure: false,
    onStopped: false,
    onSuccess: false,
    server: "https://example.invalid",
    topic: "test",
  };
  const initial = {
    ...snapshot({runId: null, state: "idle", stdout: ""}),
    cwd: "/work",
    exitCode: null,
  };
  const fetch = async (url, options = {}) => {
    calls.push([url, options.method || "GET"]);
    const override = fetchOverride?.(url, options);
    if (override !== undefined) return override;
    if (url === "/api/token") return response({token: "request-token"});
    if (url === "/api/status") return response(initial);
    if (url === "/api/runs") return response({runs});
    if (url === "/api/settings/notifications") return response(settings);
    if (url === "/api/validate") {
      return response(validation.body, validation.status);
    }
    const match = url.match(/^\/api\/runs\/(\d+)$/);
    if (match) return response(details[Number(match[1])]);
    throw new Error(`unexpected request: ${url}`);
  };
  let intervalId = 0;
  const context = {
    console,
    document,
    fetch,
    navigator: {clipboard: {async writeText() {}}},
    runnerOutputClipboard: {
      formatOutput(stdout, stderr) {
        return `stdout:\n${stdout}\n\nstderr:\n${stderr}`;
      },
      async write() {},
      async writeText() {},
    },
    setTimeout,
    clearTimeout,
    window: {
      clearInterval() {},
      clearTimeout,
      setInterval() { intervalId += 1; return intervalId; },
      setTimeout,
    },
  };
  vm.runInNewContext(appSource, context, {filename: "app.js"});

  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (calls.some(([url]) => url === "/api/settings/notifications")) break;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.ok(calls.some(([url]) => url === "/api/settings/notifications"));
  return {calls, elements};
}

function selectedRun(elements) {
  return elements["run-list"].children.find(
    (element) => element.className.includes("selected"),
  );
}

test("validation preserves a selected failed resumable run through refresh", async () => {
  const failed = snapshot({
    runId: 2,
    state: "failed",
    stdout: "failed run output",
    resumable: true,
    checkpoint: {name: "safe-step"},
    attempts: [{number: 1, state: "failed", exitCode: 1, resumedFrom: null}],
  });
  const {elements} = await loadApp({
    runs: [
      {runId: 1, state: "success", cwd: "/work/run-1"},
      {runId: 2, state: "failed", cwd: "/work/run-2"},
    ],
    details: {1: snapshot({runId: 1, state: "success", stdout: "old"}), 2: failed},
    validation: {
      status: 422,
      body: {
        error: "workflow validation failed",
        validation: [{line: 4, column: 2, message: "broken preview"}],
      },
    },
  });

  assert.match(selectedRun(elements).textContent, /^#2/);
  assert.equal(elements.stdout.textContent, "failed run output");
  assert.equal(elements.resume.disabled, false);
  assert.equal(elements["recovery-panel"].hidden, false);

  await elements.validate.dispatch("click");

  assert.match(selectedRun(elements).textContent, /^#2/);
  assert.equal(elements.status.textContent, "failed");
  assert.equal(elements.stdout.textContent, "failed run output");
  assert.equal(elements.resume.disabled, false);
  assert.equal(elements["recovery-panel"].hidden, false);
  assert.equal(elements["validation-panel"].hidden, false);
  assert.equal(elements.validation.children[0].textContent, "Line 4:2: broken preview");

  await selectedRun(elements).dispatch("click");

  assert.equal(elements.stdout.textContent, "failed run output");
  assert.equal(elements.resume.disabled, false);
  assert.equal(elements.validation.children[0].textContent, "Line 4:2: broken preview");
});

test("validation preserves another selected running run through refresh", async () => {
  const running = snapshot({
    runId: 1,
    state: "running",
    stdout: "live output",
  });
  const {elements} = await loadApp({
    runs: [
      {runId: 1, state: "running", cwd: "/work/run-1"},
      {runId: 2, state: "failed", cwd: "/work/run-2"},
    ],
    details: {
      1: running,
      2: snapshot({runId: 2, state: "failed", stdout: "newer failed"}),
    },
    validation: {
      status: 422,
      body: {
        error: "workflow validation failed",
        validation: [{line: null, column: null, message: "preview path missing"}],
      },
    },
  });

  const runOne = elements["run-list"].children.find(
    (element) => element.textContent.startsWith("#1"),
  );
  await runOne.dispatch("click");
  assert.match(selectedRun(elements).textContent, /^#1/);
  assert.equal(elements.stop.disabled, false);

  await elements.validate.dispatch("click");

  assert.equal(elements.status.textContent, "running");
  assert.equal(elements.stdout.textContent, "live output");
  assert.equal(elements.stop.disabled, false);
  assert.equal(elements.resume.disabled, true);
  assert.equal(elements.validation.children[0].textContent, "preview path missing");

  await selectedRun(elements).dispatch("click");

  assert.match(selectedRun(elements).textContent, /^#1/);
  assert.equal(elements.stdout.textContent, "live output");
  assert.equal(elements.stop.disabled, false);
  assert.equal(elements.validation.children[0].textContent, "preview path missing");
});

test("slow run response cannot replace a newly selected run", async () => {
  const delayedRun = deferred();
  let delayRunTwo = false;
  const runOne = snapshot({runId: 1, state: "running", stdout: "run one output"});
  const runTwo = snapshot({runId: 2, state: "failed", stdout: "run two output"});
  const {calls, elements} = await loadApp({
    runs: [
      {runId: 1, state: "running", cwd: "/work/run-1"},
      {runId: 2, state: "failed", cwd: "/work/run-2"},
    ],
    details: {1: runOne, 2: runTwo},
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url) {
      if (delayRunTwo && url === "/api/runs/2") return delayedRun.promise;
      return undefined;
    },
  });

  delayRunTwo = true;
  const slowRefresh = selectedRun(elements).dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(calls.filter(([url]) => url === "/api/runs/2").length >= 2);

  const runOneButton = elements["run-list"].children.find(
    (element) => element.textContent.startsWith("#1"),
  );
  await runOneButton.dispatch("click");
  assert.match(selectedRun(elements).textContent, /^#1/);
  assert.equal(elements.stdout.textContent, "run one output");
  assert.equal(elements.stop.disabled, false);

  delayedRun.resolve(response(runTwo));
  await slowRefresh;

  assert.match(selectedRun(elements).textContent, /^#1/);
  assert.equal(elements.status.textContent, "running");
  assert.equal(elements.stdout.textContent, "run one output");
  assert.equal(elements.stop.disabled, false);
  assert.equal(elements.resume.disabled, true);
});

test("slow run action cannot replace a newly selected run", async () => {
  const delayedStop = deferred();
  const runOne = snapshot({
    runId: 1,
    state: "failed",
    stdout: "selected failed run",
    resumable: true,
    checkpoint: {name: "safe-step"},
  });
  const runTwo = snapshot({runId: 2, state: "running", stdout: "stopping run"});
  const {elements} = await loadApp({
    runs: [
      {runId: 1, state: "failed", cwd: "/work/run-1"},
      {runId: 2, state: "running", cwd: "/work/run-2"},
    ],
    details: {1: runOne, 2: runTwo},
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url) {
      if (url === "/api/runs/2/stop") return delayedStop.promise;
      return undefined;
    },
  });

  const slowStop = elements.stop.dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  const runOneButton = elements["run-list"].children.find(
    (element) => element.textContent.startsWith("#1"),
  );
  await runOneButton.dispatch("click");

  delayedStop.resolve(response({
    ...runTwo,
    state: "stopped",
    exitCode: -15,
  }, 202));
  await slowStop;

  assert.match(selectedRun(elements).textContent, /^#1/);
  assert.equal(elements.status.textContent, "failed");
  assert.equal(elements.stdout.textContent, "selected failed run");
  assert.equal(elements.stop.disabled, true);
  assert.equal(elements.resume.disabled, false);
});

test("slow validation response cannot replace a newer validation result", async () => {
  const delayedValidation = deferred();
  let validationCalls = 0;
  const {elements} = await loadApp({
    runs: [{runId: 1, state: "success", cwd: "/work/run-1"}],
    details: {1: snapshot({runId: 1, state: "success", stdout: "done"})},
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url) {
      if (url !== "/api/validate") return undefined;
      validationCalls += 1;
      if (validationCalls === 1) return delayedValidation.promise;
      return response({
        error: "workflow validation failed",
        validation: [{line: 8, column: null, message: "newer result"}],
      }, 422);
    },
  });

  const slowValidation = elements.validate.dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  await elements.validate.dispatch("click");
  assert.equal(elements.validation.children[0].textContent, "Line 8: newer result");

  delayedValidation.resolve(response({
    error: "workflow validation failed",
    validation: [{line: 2, column: null, message: "stale result"}],
  }, 422));
  await slowValidation;

  assert.equal(elements.validation.children.length, 1);
  assert.equal(elements.validation.children[0].textContent, "Line 8: newer result");
});
