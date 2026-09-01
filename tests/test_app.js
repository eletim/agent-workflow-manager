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

  prepend(...children) {
    this.children.unshift(...children);
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  getAttribute(name) {
    return this.attributes[name];
  }

  focus() {}

  select() {}

  setSelectionRange(start, end) {
    this.selection = [start, end];
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
  cwd = `/work/run-${runId}`,
  args = [],
  code = `print("run-${runId}")`,
}) {
  return {
    args,
    attempts,
    checkpoint,
    code,
    cwd,
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

async function waitFor(predicate) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail("condition was not met");
}

async function loadApp({
  runs,
  details,
  validation,
  fetchOverride = null,
  clipboardOverride = null,
}) {
  const ids = [
    "code", "working-directory", "run-arguments", "active-context", "run-list",
    "runs-empty", "new-run", "run", "resume", "validate", "stop", "status", "stdout",
    "stderr", "output-copy", "exit-code", "progress", "progress-empty",
    "recovery-panel", "recovery-summary", "attempt-history", "validation-panel",
    "validation-success", "validation", "guide-dialog", "guide-open", "guide-close", "guide-copy",
    "guide-content", "manual-copy-dialog", "manual-copy-content",
    "manual-copy-close", "notification-settings", "notifications-enabled",
    "notify-success", "notify-failure", "notify-stopped", "notify-server",
    "notify-topic", "replacement-token", "credential-status", "settings-message",
    "save-settings", "test-notification",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element()]));
  elements.favicon = new Element();
  elements.favicon.setAttribute("href", "/favicon.svg");
  elements["validation-panel"].hidden = true;
  elements["validation-success"].hidden = true;
  elements["validation-success"].textContent = "✓ Valid";
  const calls = [];
  const eventSources = [];
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
    if (url === "/favicon.svg") {
      return response('<svg xmlns="http://www.w3.org/2000/svg"></svg>');
    }
    if (url === "/api/settings/notifications") return response(settings);
    if (url === "/api/validate") {
      return response(validation.body, validation.status);
    }
    const match = url.match(/^\/api\/runs\/(\d+)$/);
    if (match) return response(details[Number(match[1])]);
    throw new Error(`unexpected request: ${url}`);
  };
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.listeners = new Map();
      eventSources.push(this);
    }

    addEventListener(type, listener) {
      const listeners = this.listeners.get(type) || [];
      listeners.push(listener);
      this.listeners.set(type, listeners);
    }

    emit(type) {
      for (const listener of this.listeners.get(type) || []) listener({type});
    }
  }
  const context = {
    console,
    document,
    fetch,
    navigator: {clipboard: {async writeText() {}}},
    runnerOutputClipboard: clipboardOverride || {
      formatOutput(stdout, stderr) {
        return `stdout:\n${stdout}\n\nstderr:\n${stderr}`;
      },
      async write() {},
      async writeText() {},
    },
    setTimeout,
    clearTimeout,
    window: {
      clearTimeout,
      EventSource: FakeEventSource,
      setInterval() { assert.fail("fixed polling must not be used"); },
      setTimeout,
    },
  };
  vm.runInNewContext(appSource, context, {filename: "app.js"});

  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (calls.some(([url]) => url === "/api/settings/notifications")) break;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.ok(calls.some(([url]) => url === "/api/settings/notifications"));
  assert.equal(eventSources.length, 1);
  assert.equal(eventSources[0].url, "/api/events");
  return {calls, elements, eventSource: eventSources[0]};
}

function selectedRun(elements) {
  return elements["run-list"].children.find(
    (element) => element.className.includes("selected"),
  );
}

function runItem(elements, runId) {
  return elements["run-list"].children.find(
    (element) => element.dataset.runId === String(runId),
  );
}

function markerState(elements, runId) {
  const item = runItem(elements, runId);
  assert.equal(item.children[0].className, "run-state-marker");
  assert.equal(item.children[0].attributes["aria-hidden"], "true");
  return item.dataset.state;
}

function faviconIsRunning(elements) {
  return decodeURIComponent(elements.favicon.getAttribute("href"))
    .includes('id="running-badge"');
}

test("favicon starts idle when there are no runs", async () => {
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {status: 200, body: {validation: []}},
  });

  assert.equal(elements.favicon.getAttribute("href"), "/favicon.svg");
  assert.equal(faviconIsRunning(elements), false);
});

test("favicon badge reflects any running run independently of selection", async () => {
  const runs = [
    {runId: 1, state: "running", cwd: "/work/run-1"},
    {runId: 2, state: "success", cwd: "/work/run-2"},
  ];
  const {elements} = await loadApp({
    runs,
    details: {
      1: snapshot({runId: 1, state: "running", stdout: "active"}),
      2: snapshot({runId: 2, state: "success", stdout: "done"}),
    },
    validation: {status: 200, body: {validation: []}},
  });
  await waitFor(() => faviconIsRunning(elements));

  await runItem(elements, 2).dispatch("click");

  assert.ok(runItem(elements, 2).className.includes("selected"));
  assert.equal(faviconIsRunning(elements), true);
});

test("favicon badge clears through SSE when the final running run stops", async () => {
  const runs = [
    {runId: 1, state: "success", cwd: "/work/run-1"},
    {runId: 2, state: "running", cwd: "/work/run-2"},
  ];
  const details = {
    1: snapshot({runId: 1, state: "success", stdout: "done"}),
    2: snapshot({runId: 2, state: "running", stdout: "active"}),
  };
  const {elements, eventSource} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
  });
  await waitFor(() => faviconIsRunning(elements));

  runs[1] = {...runs[1], state: "stopped"};
  details[2] = {...details[2], state: "stopped", exitCode: -15};
  eventSource.emit("runner-change");
  await waitFor(() => !faviconIsRunning(elements));

  assert.equal(elements.favicon.getAttribute("href"), "/favicon.svg");
});

test("run rows render independent decorative indicators from textual states", async () => {
  const runs = [
    {runId: 1, state: "running", cwd: "/work/run-1"},
    {runId: 2, state: "success", cwd: "/work/run-2"},
    {runId: 3, state: "failed", cwd: "/work/run-3"},
    {runId: 4, state: "stopped", cwd: "/work/run-4"},
    {runId: 5, state: "suspended", cwd: "/work/run-5"},
  ];
  const details = Object.fromEntries(runs.map((run) => [
    run.runId,
    snapshot({runId: run.runId, state: run.state, stdout: run.state}),
  ]));
  const {elements} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
  });

  for (const run of runs) {
    assert.equal(markerState(elements, run.runId), run.state);
    assert.match(runItem(elements, run.runId).textContent, new RegExp(`  ${run.state}  `));
  }
});

test("state changes and selection update independently", async () => {
  const runs = [
    {runId: 1, state: "running", cwd: "/work/run-1"},
    {runId: 2, state: "success", cwd: "/work/run-2"},
  ];
  const details = {
    1: snapshot({runId: 1, state: "running", stdout: "active"}),
    2: snapshot({runId: 2, state: "success", stdout: "done"}),
  };
  const {elements, eventSource} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
  });

  await runItem(elements, 1).dispatch("click");
  assert.ok(runItem(elements, 1).className.includes("selected"));

  for (const state of ["success", "failed", "stopped", "suspended"]) {
    runs[0] = {...runs[0], state};
    details[1] = {...details[1], state};
    eventSource.emit("runner-change");
    await waitFor(() => markerState(elements, 1) === state);

    assert.ok(runItem(elements, 1).className.includes("selected"));
    assert.equal(markerState(elements, 2), "success");
  }

  await runItem(elements, 2).dispatch("click");
  assert.ok(runItem(elements, 2).className.includes("selected"));
  assert.equal(markerState(elements, 1), "suspended");
  assert.equal(markerState(elements, 2), "success");
});

test("stale refresh cannot roll a newer run indicator back", async () => {
  const delayedRuns = deferred();
  let delayNextRuns = false;
  const runs = [{runId: 1, state: "running", cwd: "/work/run-1"}];
  const details = {1: snapshot({runId: 1, state: "running", stdout: "active"})};
  const {elements, eventSource} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url) {
      if (delayNextRuns && url === "/api/runs") {
        delayNextRuns = false;
        return delayedRuns.promise;
      }
      return undefined;
    },
  });

  delayNextRuns = true;
  const staleRefresh = runItem(elements, 1).dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));

  runs[0] = {...runs[0], state: "success"};
  details[1] = {...details[1], state: "success", exitCode: 0};
  eventSource.emit("runner-change");
  await waitFor(() => markerState(elements, 1) === "success");
  assert.equal(faviconIsRunning(elements), false);

  delayedRuns.resolve(response({
    runs: [{runId: 1, state: "running", cwd: "/work/run-1"}],
  }));
  await staleRefresh;

  assert.equal(markerState(elements, 1), "success");
  assert.match(runItem(elements, 1).textContent, /  success  /);
  assert.equal(faviconIsRunning(elements), false);
});

test("initial state loads through read APIs before any SSE event", async () => {
  const running = snapshot({runId: 1, state: "running", stdout: "already running"});
  const {calls, elements} = await loadApp({
    runs: [{runId: 1, state: "running", cwd: "/work/run-1"}],
    details: {1: running},
    validation: {status: 200, body: {validation: []}},
  });

  assert.equal(elements.stdout.textContent, "already running");
  assert.ok(calls.some(([url]) => url === "/api/status"));
  assert.ok(calls.some(([url]) => url === "/api/runs/1"));
});

test("SSE changes refresh state, output, and progress without polling", async () => {
  const runs = [{runId: 1, state: "running", cwd: "/work/run-1"}];
  const details = {1: snapshot({runId: 1, state: "running", stdout: "starting"})};
  const {calls, elements, eventSource} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
  });
  const callsBeforeEvent = calls.length;

  runs[0] = {...runs[0], state: "success"};
  details[1] = {
    ...details[1],
    state: "success",
    stdout: "starting\nfinished\n",
    exitCode: 0,
    progress: [{name: "deploy", status: "completed"}],
  };
  eventSource.emit("runner-change");
  await waitFor(() => elements.status.textContent === "success");

  assert.equal(elements.stdout.textContent, "starting\nfinished\n");
  assert.equal(elements.progress.children[0].children[1].children[0].textContent, "deploy");
  assert.ok(calls.slice(callsBeforeEvent).some(([url]) => url === "/api/runs"));
  assert.equal(elements.stop.disabled, true);
});

test("SSE bursts coalesce to one active and one pending refresh", async () => {
  const delayedDetail = deferred();
  let delayNextDetail = false;
  let detailWasDelayed = false;
  const runs = [{runId: 1, state: "running", cwd: "/work/run-1"}];
  const details = {1: snapshot({runId: 1, state: "running", stdout: "initial"})};
  const {calls, elements, eventSource} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url) {
      if (delayNextDetail && !detailWasDelayed && url === "/api/runs/1") {
        detailWasDelayed = true;
        return delayedDetail.promise;
      }
      return undefined;
    },
  });
  const callsBeforeBurst = calls.length;
  delayNextDetail = true;

  eventSource.emit("runner-change");
  await waitFor(() => detailWasDelayed);
  for (let event = 0; event < 100; event += 1) {
    eventSource.emit("runner-change");
  }

  assert.equal(
    calls.slice(callsBeforeBurst).filter(([url]) => url === "/api/runs").length,
    1,
  );
  assert.equal(
    calls.slice(callsBeforeBurst).filter(([url]) => url === "/api/runs/1").length,
    1,
  );

  runs[0] = {...runs[0], state: "success"};
  details[1] = {
    ...details[1],
    state: "success",
    stdout: "latest authoritative output",
    exitCode: 0,
  };
  delayedDetail.resolve(response(snapshot({
    runId: 1,
    state: "running",
    stdout: "stale in-flight output",
  })));
  await waitFor(() => elements.stdout.textContent === "latest authoritative output");

  assert.equal(
    calls.slice(callsBeforeBurst).filter(([url]) => url === "/api/runs").length,
    2,
  );
  assert.equal(
    calls.slice(callsBeforeBurst).filter(([url]) => url === "/api/runs/1").length,
    2,
  );
  assert.equal(elements.status.textContent, "success");
});

test("SSE refresh preserves the selected run while multiple runs change", async () => {
  const runs = [
    {runId: 1, state: "running", cwd: "/work/run-1"},
    {runId: 2, state: "running", cwd: "/work/run-2"},
  ];
  const details = {
    1: snapshot({runId: 1, state: "running", stdout: "one"}),
    2: snapshot({runId: 2, state: "running", stdout: "two"}),
  };
  const {elements, eventSource} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
  });
  const runOne = elements["run-list"].children.find(
    (element) => element.textContent.startsWith("#1"),
  );
  await runOne.dispatch("click");

  runs[1] = {...runs[1], state: "success"};
  details[2] = {...details[2], state: "success", stdout: "two done", exitCode: 0};
  details[1] = {...details[1], stdout: "one still selected"};
  eventSource.emit("runner-change");
  await waitFor(() => elements.stdout.textContent === "one still selected");

  assert.match(selectedRun(elements).textContent, /^#1/);
  const runTwo = elements["run-list"].children.find(
    (element) => element.textContent.startsWith("#2"),
  );
  assert.equal(runTwo.dataset.state, "success");
});

test("EventSource reconnect reconciles authoritative state", async () => {
  const runs = [{runId: 1, state: "running", cwd: "/work/run-1"}];
  const details = {1: snapshot({runId: 1, state: "running", stdout: "before gap"})};
  const {elements, eventSource} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
  });

  details[1] = {...details[1], stdout: "after reconnect"};
  eventSource.emit("open");
  await waitFor(() => elements.stdout.textContent === "after reconnect");

  assert.equal(elements.status.textContent, "running");
});

test("successful validation is explicit when no run exists", async () => {
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {status: 200, body: {validation: []}},
  });

  assert.equal(elements["validation-panel"].hidden, true);

  await elements.validate.dispatch("click");

  assert.equal(elements["validation-panel"].hidden, false);
  assert.equal(elements["validation-success"].hidden, false);
  assert.equal(elements["validation-success"].textContent, "✓ Valid");
  assert.equal(elements.validation.hidden, true);
  assert.equal(elements.validation.children.length, 0);
});

test("validation issues replace success feedback", async () => {
  let validationCalls = 0;
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url) {
      if (url !== "/api/validate") return undefined;
      validationCalls += 1;
      if (validationCalls === 1) return response({validation: []});
      return response({
        error: "workflow validation failed",
        validation: [{line: 3, column: null, message: "fix this import"}],
      }, 422);
    },
  });

  await elements.validate.dispatch("click");
  assert.equal(elements["validation-success"].hidden, false);

  await elements.validate.dispatch("click");

  assert.equal(elements["validation-success"].hidden, true);
  assert.equal(elements.validation.hidden, false);
  assert.equal(elements.validation.children[0].textContent, "Line 3: fix this import");
});

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
  // Viewing an existing run: Validate is a draft-only action, blocked until
  // "New run" is clicked, so it can never submit this run's own snapshot.
  assert.equal(elements.validate.disabled, true);

  await elements["new-run"].dispatch("click");
  assert.equal(elements.validate.disabled, false);
  await elements.validate.dispatch("click");

  assert.equal(elements["validation-panel"].hidden, false);
  assert.equal(elements.validation.children[0].textContent, "Line 4:2: broken preview");

  await runItem(elements, 2).dispatch("click");

  assert.match(selectedRun(elements).textContent, /^#2/);
  assert.equal(elements.stdout.textContent, "failed run output");
  assert.equal(elements.resume.disabled, false);
  assert.equal(elements["recovery-panel"].hidden, false);
  // Validation state is independent of run selection: it survives switching
  // through the draft and back to the run without being cleared or applied.
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
    validation: {status: 200, body: {validation: []}},
  });

  const runOne = elements["run-list"].children.find(
    (element) => element.textContent.startsWith("#1"),
  );
  await runOne.dispatch("click");
  assert.match(selectedRun(elements).textContent, /^#1/);
  assert.equal(elements.stop.disabled, false);
  assert.equal(elements.validate.disabled, true);

  await elements["new-run"].dispatch("click");
  assert.equal(elements.validate.disabled, false);
  await elements.validate.dispatch("click");

  assert.equal(elements["validation-success"].hidden, false);
  assert.equal(elements["validation-success"].textContent, "✓ Valid");

  await runOne.dispatch("click");

  assert.match(selectedRun(elements).textContent, /^#1/);
  assert.equal(elements.stdout.textContent, "live output");
  assert.equal(elements.stop.disabled, false);
  assert.equal(elements["validation-success"].hidden, false);
  assert.equal(elements["validation-success"].textContent, "✓ Valid");
});

test("slow SSE refresh cannot replace a newly selected run", async () => {
  const delayedRun = deferred();
  let delayRunTwo = false;
  const runOne = snapshot({runId: 1, state: "running", stdout: "run one output"});
  const runTwo = snapshot({runId: 2, state: "failed", stdout: "run two output"});
  const {calls, elements, eventSource} = await loadApp({
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
  eventSource.emit("runner-change");
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
  await new Promise((resolve) => setImmediate(resolve));

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
      return response({validation: []});
    },
  });
  await elements["new-run"].dispatch("click");

  const slowValidation = elements.validate.dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  await elements.validate.dispatch("click");
  assert.equal(elements["validation-success"].hidden, false);
  assert.equal(elements["validation-success"].textContent, "✓ Valid");

  delayedValidation.resolve(response({
    error: "workflow validation failed",
    validation: [{line: 2, column: null, message: "stale result"}],
  }, 422));
  await slowValidation;

  assert.equal(elements["validation-success"].hidden, false);
  assert.equal(elements["validation-success"].textContent, "✓ Valid");
  assert.equal(elements.validation.children.length, 0);
});

test("manual output copy preserves the payload attempted before a run switch", async () => {
  const delayedCopy = deferred();
  const attempted = [];
  const clipboard = {
    formatOutput(stdout, stderr) {
      return `stdout:\n${stdout}\n\nstderr:\n${stderr}`;
    },
    async writeText(text) {
      attempted.push(text);
      await delayedCopy.promise;
      throw new Error("copy denied");
    },
  };
  const runOne = snapshot({runId: 1, state: "success", stdout: "run one"});
  const runTwo = snapshot({runId: 2, state: "failed", stdout: "run two"});
  const {elements} = await loadApp({
    runs: [
      {runId: 1, state: "success", cwd: "/work/run-1"},
      {runId: 2, state: "failed", cwd: "/work/run-2"},
    ],
    details: {1: runOne, 2: runTwo},
    validation: {status: 200, body: {validation: []}},
    clipboardOverride: clipboard,
  });

  const copy = elements["output-copy"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  const attemptedRunTwo = "stdout:\nrun two\n\nstderr:\nstderr-2";
  assert.deepEqual(attempted, [attemptedRunTwo]);

  const runOneButton = elements["run-list"].children.find(
    (element) => element.textContent.startsWith("#1"),
  );
  await runOneButton.dispatch("click");
  assert.equal(elements.stdout.textContent, "run one");

  delayedCopy.resolve();
  await copy;

  assert.equal(elements["output-copy"].textContent, "Copy manually");
  assert.equal(elements["manual-copy-content"].value, attemptedRunTwo);
  assert.deepEqual(
    elements["manual-copy-content"].selection,
    [0, attemptedRunTwo.length],
  );
});

// Issue #45: Working directory / Arguments / Python must always reflect the
// currently selected run's own immutable snapshot, never another run's
// values and never the new-run draft.

test("selecting a run renders its own cwd/args/code, never another run's values", async () => {
  const runA = snapshot({
    runId: 1,
    state: "success",
    stdout: "A output",
    cwd: "/tmp/awm-run-a",
    args: ["A-ARG-1", "A-ARG-2"],
    code: "print('RUN=A')",
  });
  const runB = snapshot({
    runId: 2,
    state: "success",
    stdout: "B output",
    cwd: "/tmp/awm-run-b",
    args: ["B-ARG-1"],
    code: "print('RUN=B')",
  });
  const {elements} = await loadApp({
    runs: [
      {runId: 1, state: "success", cwd: "/tmp/awm-run-a"},
      {runId: 2, state: "success", cwd: "/tmp/awm-run-b"},
    ],
    details: {1: runA, 2: runB},
    validation: {status: 200, body: {validation: []}},
  });

  // The most recent run (B) is auto-selected and shown read-only on load.
  assert.equal(elements["working-directory"].value, "/tmp/awm-run-b");
  assert.equal(elements["run-arguments"].value, "B-ARG-1");
  assert.equal(elements.code.value, "print('RUN=B')");
  assert.equal(elements["working-directory"].readOnly, true);
  assert.equal(elements["run-arguments"].readOnly, true);
  assert.equal(elements.code.readOnly, true);

  await runItem(elements, 1).dispatch("click");
  assert.equal(elements["working-directory"].value, "/tmp/awm-run-a");
  assert.equal(elements["run-arguments"].value, "A-ARG-1\nA-ARG-2");
  assert.equal(elements.code.value, "print('RUN=A')");

  await runItem(elements, 2).dispatch("click");
  assert.equal(elements["working-directory"].value, "/tmp/awm-run-b");
  assert.equal(elements["run-arguments"].value, "B-ARG-1");
  assert.equal(elements.code.value, "print('RUN=B')");

  await runItem(elements, 1).dispatch("click");
  assert.equal(elements["working-directory"].value, "/tmp/awm-run-a");
  assert.equal(elements["run-arguments"].value, "A-ARG-1\nA-ARG-2");
  assert.equal(elements.code.value, "print('RUN=A')");
});

test("New run restores the retained draft unchanged after switching between runs", async () => {
  const runA = snapshot({
    runId: 1, state: "success", stdout: "A", cwd: "/work/run-1",
    args: ["a"], code: "print('A')",
  });
  const runB = snapshot({
    runId: 2, state: "success", stdout: "B", cwd: "/work/run-2",
    args: ["b"], code: "print('B')",
  });
  const {elements} = await loadApp({
    runs: [
      {runId: 1, state: "success", cwd: "/work/run-1"},
      {runId: 2, state: "success", cwd: "/work/run-2"},
    ],
    details: {1: runA, 2: runB},
    validation: {status: 200, body: {validation: []}},
  });

  await elements["new-run"].dispatch("click");
  assert.equal(elements["working-directory"].readOnly, false);
  elements["working-directory"].value = "/tmp/draft-dir";
  elements["run-arguments"].value = "draft-arg-1\ndraft-arg-2";
  elements.code.value = "print('draft')";

  await runItem(elements, 1).dispatch("click");
  assert.equal(elements["working-directory"].value, "/work/run-1");

  await runItem(elements, 2).dispatch("click");
  assert.equal(elements["working-directory"].value, "/work/run-2");

  await elements["new-run"].dispatch("click");

  assert.equal(elements["working-directory"].value, "/tmp/draft-dir");
  assert.equal(elements["run-arguments"].value, "draft-arg-1\ndraft-arg-2");
  assert.equal(elements.code.value, "print('draft')");
  assert.equal(elements["working-directory"].readOnly, false);
  assert.equal(elements.run.disabled, false);
  assert.equal(selectedRun(elements), undefined);
});

test("selecting a run never calls a mutating endpoint", async () => {
  const runA = snapshot({runId: 1, state: "success", stdout: "A"});
  const runB = snapshot({
    runId: 2, state: "failed", stdout: "B",
    resumable: true, checkpoint: {name: "step"},
  });
  const {calls, elements} = await loadApp({
    runs: [
      {runId: 1, state: "success", cwd: "/work/run-1"},
      {runId: 2, state: "failed", cwd: "/work/run-2"},
    ],
    details: {1: runA, 2: runB},
    validation: {status: 200, body: {validation: []}},
  });

  const callsBefore = calls.length;
  await runItem(elements, 1).dispatch("click");
  await runItem(elements, 2).dispatch("click");
  await elements["new-run"].dispatch("click");

  const madeSinceSelecting = calls.slice(callsBefore);
  assert.ok(madeSinceSelecting.length > 0);
  assert.ok(madeSinceSelecting.every(([, method]) => (method || "GET") === "GET"));
});

test("Run submission after returning to New run uses the draft, not a viewed run's snapshot", async () => {
  const runA = snapshot({
    runId: 1, state: "success", stdout: "A", cwd: "/work/run-1",
    args: ["a"], code: "print('A')",
  });
  let runRequestBody = null;
  const {elements} = await loadApp({
    runs: [{runId: 1, state: "success", cwd: "/work/run-1"}],
    details: {1: runA},
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url, options) {
      if (url !== "/api/run") return undefined;
      runRequestBody = JSON.parse(options.body);
      return response({
        runId: 2,
        state: "running",
        stdout: "",
        stderr: "",
        exitCode: null,
        progress: [],
        validation: [],
        cwd: "/tmp/draft-dir",
        args: ["draft-arg"],
        code: "print('draft')",
        checkpoint: null,
        attempts: [],
        suspensionReason: null,
        resumable: false,
      });
    },
  });

  // Viewing run #1: Run is blocked until an explicit New run.
  assert.equal(elements.run.disabled, true);

  await elements["new-run"].dispatch("click");
  elements["working-directory"].value = "/tmp/draft-dir";
  elements["run-arguments"].value = "draft-arg";
  elements.code.value = "print('draft')";

  await elements.run.dispatch("click");

  assert.deepEqual(runRequestBody, {
    code: "print('draft')",
    cwd: "/tmp/draft-dir",
    args: ["draft-arg"],
  });
});

test("a delayed run-detail response cannot overwrite fields belonging to a newer selection", async () => {
  const delayedRun = deferred();
  let delayRunTwo = false;
  const runOne = snapshot({
    runId: 1, state: "running", stdout: "A", cwd: "/work/run-1",
    args: ["a"], code: "print('A')",
  });
  const runTwo = snapshot({
    runId: 2, state: "failed", stdout: "B", cwd: "/work/run-2",
    args: ["b"], code: "print('B')",
  });
  const {elements, eventSource} = await loadApp({
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
  eventSource.emit("runner-change");
  await new Promise((resolve) => setImmediate(resolve));

  await runItem(elements, 1).dispatch("click");
  assert.equal(elements["working-directory"].value, "/work/run-1");
  assert.equal(elements["run-arguments"].value, "a");
  assert.equal(elements.code.value, "print('A')");

  delayedRun.resolve(response(runTwo));
  await new Promise((resolve) => setImmediate(resolve));

  assert.match(selectedRun(elements).textContent, /^#1/);
  assert.equal(elements["working-directory"].value, "/work/run-1");
  assert.equal(elements["run-arguments"].value, "a");
  assert.equal(elements.code.value, "print('A')");
});

test("New run is a no-op while already drafting and never discards in-progress edits", async () => {
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {status: 200, body: {validation: []}},
  });

  elements["working-directory"].value = "/tmp/still-typing";
  elements["run-arguments"].value = "still-typing-arg";
  elements.code.value = "print('still typing')";

  await elements["new-run"].dispatch("click");

  assert.equal(elements["working-directory"].value, "/tmp/still-typing");
  assert.equal(elements["run-arguments"].value, "still-typing-arg");
  assert.equal(elements.code.value, "print('still typing')");
});

test("a run auto-selected via SSE while drafting captures in-progress edits first", async () => {
  const runs = [];
  const details = {};
  const {elements, eventSource} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
  });

  // No runs exist yet, so the fields hold an untouched draft nobody submitted.
  elements["working-directory"].value = "/tmp/in-progress-draft";
  elements["run-arguments"].value = "in-progress-arg";
  elements.code.value = "print('in progress')";

  // Another session starts a run concurrently; this client only learns about
  // it through the next SSE-triggered refresh, not through any explicit
  // action of its own.
  runs.push({runId: 9, state: "running", cwd: "/work/run-9"});
  details[9] = snapshot({
    runId: 9, state: "running", stdout: "elsewhere",
    cwd: "/work/run-9", args: ["x"], code: "print('elsewhere')",
  });
  eventSource.emit("runner-change");
  await waitFor(() => selectedRun(elements) !== undefined);

  assert.equal(elements["working-directory"].value, "/work/run-9");

  await elements["new-run"].dispatch("click");

  assert.equal(elements["working-directory"].value, "/tmp/in-progress-draft");
  assert.equal(elements["run-arguments"].value, "in-progress-arg");
  assert.equal(elements.code.value, "print('in progress')");
});
