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
const logDisplaySource = fs.readFileSync(path.join(
  __dirname,
  "..",
  "src",
  "purplemux_client",
  "web_static",
  "log-display.js",
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
  stdoutEntries = [],
  stderrEntries = [],
  outline = [],
  progress = null,
  attempts = [],
  cwd = `/work/run-${runId}`,
  args = [],
  code = `print("run-${runId}")`,
  resources = [],
  resourceCleanupStatus = resources.length ? "retained" : "cleaned",
  executionContext = null,
  mode = undefined,
  prompt = undefined,
}) {
  const result = {
    args,
    attempts,
    code,
    cwd,
    exitCode: state === "running" ? null : 1,
    outline,
    progress: progress || [{name: `step-${runId}`, status: "completed"}],
    runId,
    state,
    stderr: `stderr-${runId}`,
    stderrEntries,
    stdout,
    stdoutEntries,
    validation: [],
    dryRun: null,
    dryRunEligible: true,
    dryRunIssues: [],
    findings: [],
    resources,
    resourceCleanupStatus,
    executionContext,
    cleanupAvailable: !["idle", "running", "validation_failed"].includes(state),
  };
  if (mode !== undefined) result.mode = mode;
  if (prompt !== undefined) result.prompt = prompt;
  return result;
}

test("run details show structured repository execution identity", async () => {
  const executionContext = {
    sourceRepository: "/source/repository",
    remote: "origin",
    baseBranch: "main",
    baseRef: "origin/main",
    baseSha: "a".repeat(40),
    executionRoot: "/managed/awm-run-repository-123",
  };
  const detail = snapshot({
    runId: 1,
    state: "success",
    stdout: "done",
    executionContext,
  });
  const {elements} = await loadApp({
    runs: [{runId: 1, state: "success", executionContext}],
    details: {1: detail},
    validation: {status: 200, body: {validation: []}},
  });

  assert.match(
    elements["execution-context-details"].textContent,
    /origin\/main @ a{40}/,
  );
  assert.match(selectedRun(elements).textContent, /awm-run-repository-123/);
});

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
    "code", "run-arguments", "prompt-mode", "issue-driven-mode", "workflow-mode", "prompt-fields",
    "issue-driven-fields", "issue-driven-json", "issue-driven-python", "issue-driven-generate",
    "issue-driven-success", "issue-driven-validation",
    "workflow-fields", "prompt-agent", "prompt-cwd", "prompt-text",
    "directory-picker-open", "directory-picker-dialog", "directory-picker-close",
    "directory-picker-parent", "directory-picker-path", "directory-picker-message",
    "directory-picker-list", "directory-picker-select",
    "active-context", "run-list",
    "runs-empty", "new-run", "run", "validate", "dry-run", "stop", "cleanup", "status", "stdout",
    "stderr", "output-copy", "exit-code", "progress", "progress-empty",
    "recovery-panel", "recovery-summary", "attempt-history", "resources-panel",
    "resources-summary", "execution-context-details", "resources", "validation-panel",
    "validation-success", "validation", "outline-panel", "outline", "guide-dialog",
    "dry-run-panel", "dry-run-status", "dry-run-eligibility", "topology-findings",
    "next-mutation",
    "readiness-workspace", "readiness-provider", "run-readiness", "reconcile-readiness",
    "refresh-readiness", "readiness-summary", "readiness-details",
    "readiness-result-provider", "readiness-identity", "readiness-tab", "readiness-state",
    "readiness-cleanup", "readiness-guidance",
    "guide-open", "guide-close", "guide-copy",
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
  elements["readiness-provider"].value = "codex";
  elements["prompt-agent"].value = "codex";
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
    if (url === "/api/readiness") return response({workspaces: [], running: false, probe: null});
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
  vm.runInNewContext(logDisplaySource, context, {filename: "log-display.js"});
  vm.runInNewContext(appSource, context, {filename: "app.js"});

  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (
      calls.some(([url]) => url === "/api/settings/notifications")
      && calls.some(([url]) => url === "/api/readiness")
    ) break;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.ok(calls.some(([url]) => url === "/api/settings/notifications"));
  assert.ok(calls.some(([url]) => url === "/api/readiness"));
  assert.equal(eventSources.length, 1);
  assert.equal(eventSources[0].url, "/api/events");
  return {
    calls,
    elements,
    eventSource: eventSources[0],
    logDisplay: context.runnerLogDisplay,
  };
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

function outlineLabels(elements) {
  return elements.outline.children.map((item) => item.children[1].textContent);
}

test("Prompt mode shows only one-shot inputs and submits them directly", async () => {
  let submitted = null;
  const prompt = {agent: "claude-code", cwd: "/work/project", prompt: "Fix it"};
  const result = {
    ...snapshot({runId: 1, state: "running", stdout: "", mode: "prompt", prompt}),
    code: null,
    cleanupAvailable: false,
  };
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {body: {}, status: 200},
    fetchOverride(url, options) {
      if (url !== "/api/prompt") return undefined;
      submitted = JSON.parse(options.body);
      return response(result, 202);
    },
  });

  await elements["prompt-mode"].dispatch("click");
  assert.equal(elements["prompt-fields"].hidden, false);
  assert.equal(elements["workflow-fields"].hidden, true);
  assert.equal(elements.validate.hidden, true);
  assert.equal(elements["dry-run"].hidden, true);
  assert.equal(elements.cleanup.hidden, true);
  assert.equal(elements["guide-open"].hidden, true);

  elements["prompt-agent"].value = prompt.agent;
  elements["prompt-cwd"].value = prompt.cwd;
  elements["prompt-text"].value = prompt.prompt;
  await elements.run.dispatch("click");

  assert.deepEqual(submitted, prompt);
  assert.match(elements["active-context"].textContent, /Prompt Run #1/);
  assert.equal(elements["prompt-text"].readOnly, true);
  assert.equal(elements.code.value.includes("PurpleMuxRuntime"), false);
});

test("Issue Driven mode generates Python before existing Static Validation", async () => {
  const generatedCode = "WORKFLOW_OUTLINE = ['generated']\nprint('ok')";
  let generatedSource = null;
  let validatedPayload = null;
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {body: {validation: [], outline: ["generated"]}, status: 200},
    fetchOverride(url, options) {
      if (url === "/api/issue-driven/generate") {
        generatedSource = JSON.parse(options.body).json;
        return response({
          config: {mode: "issue-driven"},
          generatedCode,
          issueDrivenValidation: [],
        });
      }
      if (url === "/api/validate") {
        validatedPayload = JSON.parse(options.body);
        return response({validation: [], outline: ["generated"]});
      }
      return undefined;
    },
  });

  await elements["issue-driven-mode"].dispatch("click");
  elements["issue-driven-json"].value = '{"issues":[90,89]}';
  await elements.validate.dispatch("click");

  assert.equal(elements["issue-driven-fields"].hidden, false);
  assert.equal(elements["workflow-fields"].hidden, true);
  assert.equal(generatedSource, '{"issues":[90,89]}');
  assert.equal(elements["issue-driven-python"].value, generatedCode);
  assert.deepEqual(validatedPayload, {code: generatedCode, args: []});
  assert.equal(elements["issue-driven-success"].hidden, false);
  assert.deepEqual(outlineLabels(elements), ["generated"]);
});

test("stale Issue Driven generation cannot replace newer JSON and Python", async () => {
  const first = deferred();
  const second = deferred();
  let requestNumber = 0;
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {body: {}, status: 200},
    fetchOverride(url) {
      if (url !== "/api/issue-driven/generate") return undefined;
      requestNumber += 1;
      return requestNumber === 1 ? first.promise : second.promise;
    },
  });

  await elements["issue-driven-mode"].dispatch("click");
  elements["issue-driven-json"].value = '{"issues":[90]}';
  const firstGeneration = elements["issue-driven-generate"].dispatch("click");
  elements["issue-driven-json"].value = '{"issues":[91]}';
  const secondGeneration = elements["issue-driven-generate"].dispatch("click");
  second.resolve(response({
    generatedCode: "# generated for 91",
    issueDrivenValidation: [],
  }));
  await secondGeneration;
  first.resolve(response({
    generatedCode: "# generated for 90",
    issueDrivenValidation: [],
  }));
  await firstGeneration;

  assert.equal(elements["issue-driven-json"].value, '{"issues":[91]}');
  assert.equal(elements["issue-driven-python"].value, "# generated for 91");
});

test("Issue Driven Dry Run and Run reuse the generated Python endpoints", async () => {
  const generatedCode = "WORKFLOW_DRY_RUN = 1\nprint('generated')";
  const submissions = [];
  const started = snapshot({
    runId: 12,
    state: "running",
    stdout: "",
    code: generatedCode,
  });
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {body: {}, status: 200},
    fetchOverride(url, options) {
      if (url === "/api/issue-driven/generate") {
        return response({generatedCode, issueDrivenValidation: []});
      }
      if (url === "/api/dry-run") {
        submissions.push([url, JSON.parse(options.body)]);
        return response({
          validation: [],
          outline: [],
          dryRun: {status: "complete", findings: [], nextMutation: null},
        });
      }
      if (url === "/api/run") {
        submissions.push([url, JSON.parse(options.body)]);
        return response(started, 202);
      }
      return undefined;
    },
  });

  await elements["issue-driven-mode"].dispatch("click");
  elements["issue-driven-json"].value = "{}";
  await elements["dry-run"].dispatch("click");
  await elements.run.dispatch("click");

  assert.deepEqual(submissions, [
    ["/api/dry-run", {code: generatedCode, args: []}],
    ["/api/run", {code: generatedCode, args: []}],
  ]);
  assert.match(elements["active-context"].textContent, /Workflow Run #12/);
});

test("Prompt directory picker navigates and selects its resolved current path", async () => {
  const listings = {
    "/typed/project": {
      path: "/typed/project",
      parent: "/typed",
      directories: [{name: "source", path: "/typed/project/source"}],
    },
    "/typed": {
      path: "/typed",
      parent: "/",
      directories: [{name: "project", path: "/typed/project"}],
    },
    "/typed/project/source": {
      path: "/typed/project/source",
      parent: "/typed/project",
      directories: [],
    },
  };
  const requestedPaths = [];
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {body: {}, status: 200},
    fetchOverride(url, options) {
      if (url !== "/api/directories") return undefined;
      const requestedPath = JSON.parse(options.body).path;
      requestedPaths.push(requestedPath);
      return response(listings[requestedPath]);
    },
  });

  await elements["prompt-mode"].dispatch("click");
  elements["prompt-cwd"].value = "/typed/project";
  await elements["directory-picker-open"].dispatch("click");
  await waitFor(() => elements["directory-picker-list"].children.length === 1);

  assert.equal(elements["directory-picker-path"].textContent, "/typed/project");
  assert.equal(elements["directory-picker-list"].children[0].textContent, "📁 source");
  await elements["directory-picker-parent"].dispatch("click");
  await waitFor(() => elements["directory-picker-path"].textContent === "/typed");
  await elements["directory-picker-list"].children[0].dispatch("click");
  await waitFor(() => elements["directory-picker-path"].textContent === "/typed/project");
  await elements["directory-picker-list"].children[0].dispatch("click");
  await waitFor(() => elements["directory-picker-path"].textContent.endsWith("/source"));
  assert.match(
    elements["directory-picker-list"].children[0].textContent,
    /No subdirectories/,
  );

  await elements["directory-picker-select"].dispatch("click");
  assert.equal(elements["prompt-cwd"].value, "/typed/project/source");
  assert.deepEqual(requestedPaths, [
    "/typed/project", "/typed", "/typed/project", "/typed/project/source",
  ]);
});

test("Prompt directory picker reports invalid manual paths without replacing them", async () => {
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {body: {}, status: 200},
    fetchOverride(url) {
      if (url === "/api/directories") {
        return response({error: "path is not a directory"}, 400);
      }
      return undefined;
    },
  });

  await elements["prompt-mode"].dispatch("click");
  elements["prompt-cwd"].value = "/missing";
  await elements["directory-picker-open"].dispatch("click");
  await waitFor(() => elements["directory-picker-message"].textContent !== "Loading…");

  assert.match(elements["directory-picker-message"].textContent, /not a directory/);
  assert.equal(elements["prompt-cwd"].value, "/missing");
  assert.equal(elements["directory-picker-select"].disabled, true);
});

test("Prompt history restores Prompt fields without exposing generated Python", async () => {
  const prompt = {agent: "codex", cwd: "/selected/project", prompt: "Summarize"};
  const detail = {
    ...snapshot({runId: 4, state: "success", stdout: "done", mode: "prompt", prompt}),
    code: null,
    cleanupAvailable: false,
  };
  const summary = {
    runId: 4,
    state: "success",
    cwd: prompt.cwd,
    mode: "prompt",
    prompt,
    executionContext: null,
  };

  const {elements} = await loadApp({
    runs: [summary],
    details: {4: detail},
    validation: {body: {}, status: 200},
  });

  assert.equal(elements["prompt-fields"].hidden, false);
  assert.equal(elements["workflow-fields"].hidden, true);
  assert.equal(elements["prompt-agent"].value, "codex");
  assert.equal(elements["prompt-cwd"].value, prompt.cwd);
  assert.equal(elements["prompt-text"].value, prompt.prompt);
  assert.match(selectedRun(elements).textContent, /Prompt.*selected\/project/);
  assert.equal(elements.code.value.includes("PurpleMuxRuntime"), false);
});

test("Dry Run renders topology findings and the first mutation frontier", async () => {
  const dryRunResult = {
    ...snapshot({runId: null, state: "idle", stdout: ""}),
    dryRunEligible: true,
    dryRunIssues: [],
    dryRun: {
      status: "frontier",
      stdout: "",
      stderr: "",
      findings: [{category: "github", status: "passed", message: "same-head set exhausted"}],
      nextMutation: {
        operation: "create PurpleMux tab",
        target: "ws-1/reviewer",
        preState: {tabs: []},
      },
    },
  };
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {body: {}, status: 200},
    fetchOverride(url) {
      if (url === "/api/dry-run") return response(dryRunResult);
      return undefined;
    },
  });

  await elements["dry-run"].dispatch("click");

  assert.equal(elements["dry-run-panel"].hidden, false);
  assert.match(elements["dry-run-status"].textContent, /first reachable mutation/);
  assert.equal(
    elements["topology-findings"].children[0].textContent,
    "github: same-head set exhausted",
  );
  assert.match(elements["next-mutation"].textContent, /create PurpleMux tab/);
});

test("Agent readiness runs only by explicit click and shows retained cleanup separately", async () => {
  const workspaceSnapshot = {
    workspaces: [{id: "ws-1", name: "Existing", directories: ["/repo"]}],
    running: false,
    probe: null,
  };
  const uncertain = {
    status: "unknown",
    workspaceId: "ws-1",
    workspaceName: "Existing",
    provider: "codex",
    probeName: "awm-readiness-codex-abc123",
    correlationId: "abc123",
    tabId: "tab-probe",
    readiness: "ready",
    cleanup: "unknown",
    retainedTabId: "tab-probe",
    detail: "close outcome unknown",
    guidance: "Inspect retained tab 'tab-probe'; do not retry.",
  };
  const reconciled = {
    ...uncertain,
    status: "reconciled",
    cleanup: "confirmed-absent",
    retainedTabId: null,
    detail: "probe tab is authoritatively absent",
    guidance: null,
  };
  let latest = null;
  const {calls, elements} = await loadApp({
    runs: [],
    details: {},
    validation: {body: {}, status: 200},
    fetchOverride(url, options) {
      if (url === "/api/readiness/probe") {
        latest = uncertain;
        return response({probe: uncertain});
      }
      if (url === "/api/readiness/reconcile") {
        latest = reconciled;
        return response({probe: reconciled});
      }
      if (url === "/api/readiness") {
        return response({...workspaceSnapshot, probe: latest});
      }
      return undefined;
    },
  });
  await waitFor(() => calls.some(([url]) => url === "/api/readiness"));

  assert.equal(calls.some(([url]) => url === "/api/readiness/probe"), false);
  await elements["run-readiness"].dispatch("click");

  assert.equal(calls.filter(([url]) => url === "/api/readiness/probe").length, 1);
  assert.match(elements["readiness-summary"].textContent, /not a successful/);
  assert.equal(elements["readiness-state"].textContent, "ready");
  assert.equal(elements["readiness-result-provider"].textContent, "codex");
  assert.equal(elements["readiness-cleanup"].textContent, "unknown");
  assert.equal(elements["readiness-tab"].textContent, "tab-probe");
  assert.match(elements["readiness-guidance"].textContent, /do not retry/);
  assert.equal(elements["run-readiness"].disabled, true);
  assert.equal(elements["reconcile-readiness"].hidden, false);

  await elements["reconcile-readiness"].dispatch("click");

  assert.equal(calls.filter(([url]) => url === "/api/readiness/reconcile").length, 1);
  assert.equal(elements["readiness-cleanup"].textContent, "confirmed-absent");
  assert.equal(elements["run-readiness"].disabled, false);
  assert.equal(elements["reconcile-readiness"].hidden, true);
});

test("Agent readiness is disabled when there is no existing workspace", async () => {
  const {calls, elements} = await loadApp({
    runs: [],
    details: {},
    validation: {body: {}, status: 200},
  });
  await waitFor(() => calls.some(([url]) => url === "/api/readiness"));

  assert.equal(elements["run-readiness"].disabled, true);
  assert.match(elements["readiness-summary"].textContent, /No existing/);
});

test("Agent pre-create failure does not claim readiness or cleanup", async () => {
  const failed = {
    status: "failed",
    workspaceId: "ws-1",
    workspaceName: "Existing",
    provider: "codex",
    probeName: "awm-readiness-codex-race123",
    correlationId: "race123",
    tabId: null,
    readiness: "not-observed",
    cleanup: "not-attempted",
    detail: "authoritative tab set changed",
    guidance: null,
  };
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {body: {}, status: 200},
    fetchOverride(url) {
      if (url === "/api/readiness") return response({
        workspaces: [{id: "ws-1", name: "Existing", directories: ["/repo"]}],
        running: false,
        probe: failed,
      });
      return undefined;
    },
  });

  assert.match(elements["readiness-summary"].textContent, /before tab identification/);
  assert.equal(elements["readiness-state"].textContent, "not-observed");
  assert.equal(elements["readiness-cleanup"].textContent, "not-attempted");
});

test("formats observed timestamps with relative local dates", () => {
  const context = {};
  vm.runInNewContext(logDisplaySource, context, {filename: "log-display.js"});
  const {formatObservedAt} = context.runnerLogDisplay;
  const now = new Date(2026, 8, 2, 21, 40, 0);
  const today = new Date(2026, 8, 2, 21, 34, 5).toISOString();
  const yesterday = new Date(2026, 8, 1, 23, 10, 9).toISOString();
  const older = new Date(2026, 7, 31, 9, 10, 0).toISOString();

  assert.equal(formatObservedAt(today, now), "Today 21:34:05");
  assert.equal(formatObservedAt(yesterday, now), "Yesterday 23:10:09");
  assert.equal(formatObservedAt(older, now), "8/31 09:10:00");
});

test("renders entry timestamps without changing entry text", () => {
  const context = {};
  vm.runInNewContext(logDisplaySource, context, {filename: "log-display.js"});
  const {formatObservedAt, formatOutputEntries} = context.runnerLogDisplay;
  const first = new Date(2026, 8, 2, 21, 34, 5).toISOString();
  const second = new Date(2026, 8, 2, 21, 35, 6).toISOString();
  const firstLabel = formatObservedAt(first);
  const secondLabel = formatObservedAt(second);

  assert.equal(
    formatOutputEntries([
      {observedAt: first, text: "first\npart"},
      {observedAt: second, text: "ial\nlast\n"},
    ]),
    `${firstLabel}  first\n${firstLabel}  partial\n${secondLabel}  last\n`,
  );
});

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

  for (const state of ["success", "failed", "stopped"]) {
    runs[0] = {...runs[0], state};
    details[1] = {...details[1], state};
    eventSource.emit("runner-change");
    await waitFor(() => markerState(elements, 1) === state);

    assert.ok(runItem(elements, 1).className.includes("selected"));
    assert.equal(markerState(elements, 2), "success");
  }

  await runItem(elements, 2).dispatch("click");
  assert.ok(runItem(elements, 2).className.includes("selected"));
  assert.equal(markerState(elements, 1), "stopped");
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

test("execution outline reflects matching progress and keeps dynamic progress", async () => {
  const completedAt = "2026-09-05T01:02:03.123456+00:00";
  const startedAt = "2026-09-05T01:03:04.123456+00:00";
  const failedAt = "2026-09-05T01:04:05.123456+00:00";
  const current = snapshot({
    runId: 1,
    state: "running",
    stdout: "",
    outline: ["prepare", "implement", "review", "ready PR"],
    progress: [
      {name: "prepare", status: "completed", observedAt: completedAt},
      {name: "implement", status: "started", observedAt: startedAt},
      {name: "dynamic check", status: "failed", observedAt: failedAt},
    ],
  });
  const {elements, logDisplay} = await loadApp({
    runs: [{runId: 1, state: "running", cwd: "/work/run-1"}],
    details: {1: current},
    validation: {status: 200, body: {validation: []}},
  });

  assert.equal(elements["outline-panel"].hidden, false);
  assert.deepEqual(outlineLabels(elements), ["prepare", "implement", "review", "ready PR"]);
  assert.deepEqual(
    elements.outline.children.map((item) => item.className),
    [
      "outline-item completed",
      "outline-item running",
      "outline-item pending",
      "outline-item pending",
    ],
  );
  assert.deepEqual(
    elements.progress.children.map((item) => item.children[1].children[0].textContent),
    ["prepare", "implement", "dynamic check"],
  );
  assert.deepEqual(
    elements.progress.children.map(
      (item) => item.children[1].children[1].textContent,
    ),
    [completedAt, startedAt, failedAt].map(
      (value) => logDisplay.formatObservedAt(value),
    ),
  );
  assert.deepEqual(
    elements.progress.children.map(
      (item) => item.children[1].children[1].getAttribute("datetime"),
    ),
    [completedAt, startedAt, failedAt],
  );
});

test("validation displays a valid draft outline before execution", async () => {
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {
      status: 200,
      body: {validation: [], outline: ["prepare", "review"]},
    },
  });

  assert.equal(elements["outline-panel"].hidden, true);
  await elements.validate.dispatch("click");

  assert.equal(elements["outline-panel"].hidden, false);
  assert.deepEqual(outlineLabels(elements), ["prepare", "review"]);
  assert.ok(elements.outline.children.every((item) => item.className.endsWith("pending")));
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

test("EventSource reconnect preserves authoritative historical timestamps", async () => {
  const observedAt = "2026-08-31T00:10:00.000000+00:00";
  const progressObservedAt = "2026-08-31T00:09:30.123456+00:00";
  const run = snapshot({
    runId: 1,
    state: "running",
    stdout: "recorded earlier\n",
    stdoutEntries: [{observedAt, text: "recorded earlier\n"}],
    progress: [{
      name: "historical step",
      status: "completed",
      observedAt: progressObservedAt,
    }],
  });
  const {calls, elements, eventSource, logDisplay} = await loadApp({
    runs: [{runId: 1, state: "running", cwd: "/work/run-1"}],
    details: {1: run},
    validation: {status: 200, body: {validation: []}},
  });
  const renderedBeforeReconnect = elements.stdout.textContent;
  const progressBeforeReconnect = elements.progress.children[0]
    .children[1].children[1].textContent;
  const callsBeforeReconnect = calls.length;

  eventSource.emit("open");
  await waitFor(() => calls.length > callsBeforeReconnect);

  assert.equal(
    renderedBeforeReconnect,
    `${logDisplay.formatObservedAt(observedAt)}  recorded earlier\n`,
  );
  assert.equal(elements.stdout.textContent, renderedBeforeReconnect);
  assert.equal(
    progressBeforeReconnect,
    logDisplay.formatObservedAt(progressObservedAt),
  );
  assert.equal(
    elements.progress.children[0].children[1].children[1].textContent,
    progressBeforeReconnect,
  );
  assert.equal(
    elements.progress.children[0].children[1].children[1].getAttribute("datetime"),
    progressObservedAt,
  );
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

test("validation preserves a selected failed run through refresh", async () => {
  const failed = snapshot({
    runId: 2,
    state: "failed",
    stdout: "failed run output",
    attempts: [{number: 1, state: "failed", exitCode: 1}],
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
  const runOne = snapshot({
    runId: 1, state: "running", stdout: "run one output", outline: ["run one plan"],
  });
  const runTwo = snapshot({
    runId: 2, state: "failed", stdout: "run two output", outline: ["run two plan"],
  });
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
  assert.deepEqual(outlineLabels(elements), ["run one plan"]);
  assert.equal(elements.stop.disabled, false);

  delayedRun.resolve(response(runTwo));
  await new Promise((resolve) => setImmediate(resolve));

  assert.match(selectedRun(elements).textContent, /^#1/);
  assert.equal(elements.status.textContent, "running");
  assert.equal(elements.stdout.textContent, "run one output");
  assert.deepEqual(outlineLabels(elements), ["run one plan"]);
  assert.equal(elements.stop.disabled, false);
});

test("slow run action cannot replace a newly selected run", async () => {
  const delayedStop = deferred();
  const runOne = snapshot({
    runId: 1,
    state: "failed",
    stdout: "selected failed run",
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

test("slow draft validation cannot replace a selected run outline", async () => {
  const delayedValidation = deferred();
  const selected = snapshot({
    runId: 1,
    state: "success",
    stdout: "done",
    outline: ["selected run plan"],
  });
  const {elements} = await loadApp({
    runs: [{runId: 1, state: "success", cwd: "/work/run-1"}],
    details: {1: selected},
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url) {
      if (url === "/api/validate") return delayedValidation.promise;
      return undefined;
    },
  });
  await elements["new-run"].dispatch("click");

  const pendingValidation = elements.validate.dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  await runItem(elements, 1).dispatch("click");
  delayedValidation.resolve(response({
    validation: [],
    outline: ["stale draft plan"],
  }));
  await pendingValidation;

  assert.deepEqual(outlineLabels(elements), ["selected run plan"]);
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
  const runTwo = snapshot({
    runId: 2,
    state: "failed",
    stdout: "run two",
    stdoutEntries: [{observedAt: "2026-08-31T00:10:00+00:00", text: "run two"}],
  });
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
  assert.notEqual(elements.stdout.textContent, "run two");
  assert.match(elements.stdout.textContent, /run two$/);

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

// Issue #45: Arguments / Python must always reflect the
// currently selected run's own immutable snapshot, never another run's
// values and never the new-run draft.

test("selecting a run renders its own args/code, never another run's values", async () => {
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
  assert.equal(elements["run-arguments"].value, "B-ARG-1");
  assert.equal(elements.code.value, "print('RUN=B')");
  assert.equal(elements["run-arguments"].readOnly, true);
  assert.equal(elements.code.readOnly, true);

  await runItem(elements, 1).dispatch("click");
  assert.equal(elements["run-arguments"].value, "A-ARG-1\nA-ARG-2");
  assert.equal(elements.code.value, "print('RUN=A')");

  await runItem(elements, 2).dispatch("click");
  assert.equal(elements["run-arguments"].value, "B-ARG-1");
  assert.equal(elements.code.value, "print('RUN=B')");

  await runItem(elements, 1).dispatch("click");
  assert.equal(elements["run-arguments"].value, "A-ARG-1\nA-ARG-2");
  assert.equal(elements.code.value, "print('RUN=A')");
});

test("selecting runs restores each run's timestamped output history", async () => {
  const observedA = "2026-08-30T01:00:00+00:00";
  const observedB = "2026-08-31T02:00:00+00:00";
  const runA = snapshot({
    runId: 1,
    state: "success",
    stdout: "A\n",
    stdoutEntries: [{observedAt: observedA, text: "A\n"}],
    outline: ["A plan"],
  });
  const runB = snapshot({
    runId: 2,
    state: "success",
    stdout: "B\n",
    stdoutEntries: [{observedAt: observedB, text: "B\n"}],
    outline: ["B plan"],
  });
  const {elements, logDisplay} = await loadApp({
    runs: [
      {runId: 1, state: "success", cwd: "/work/run-1"},
      {runId: 2, state: "success", cwd: "/work/run-2"},
    ],
    details: {1: runA, 2: runB},
    validation: {status: 200, body: {validation: []}},
  });

  assert.equal(
    elements.stdout.textContent,
    `${logDisplay.formatObservedAt(observedB)}  B\n`,
  );
  assert.deepEqual(outlineLabels(elements), ["B plan"]);
  await runItem(elements, 1).dispatch("click");
  assert.equal(
    elements.stdout.textContent,
    `${logDisplay.formatObservedAt(observedA)}  A\n`,
  );
  assert.deepEqual(outlineLabels(elements), ["A plan"]);
  await runItem(elements, 2).dispatch("click");
  assert.equal(
    elements.stdout.textContent,
    `${logDisplay.formatObservedAt(observedB)}  B\n`,
  );
  assert.deepEqual(outlineLabels(elements), ["B plan"]);
  await runItem(elements, 1).dispatch("click");
  assert.deepEqual(outlineLabels(elements), ["A plan"]);
});

test("New run restores the retained draft unchanged after switching between runs", async () => {
  const runA = snapshot({
    runId: 1, state: "success", stdout: "A", cwd: "/work/run-1",
    args: ["a"], code: "print('A')", outline: ["A plan"],
  });
  const runB = snapshot({
    runId: 2, state: "success", stdout: "B", cwd: "/work/run-2",
    args: ["b"], code: "print('B')", outline: ["B plan"],
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
  assert.equal(elements["outline-panel"].hidden, true);
  elements["run-arguments"].value = "draft-arg-1\ndraft-arg-2";
  elements.code.value = "print('draft')";

  await runItem(elements, 1).dispatch("click");
  assert.deepEqual(outlineLabels(elements), ["A plan"]);

  await runItem(elements, 2).dispatch("click");

  await elements["new-run"].dispatch("click");

  assert.equal(elements["run-arguments"].value, "draft-arg-1\ndraft-arg-2");
  assert.equal(elements.code.value, "print('draft')");
  assert.equal(elements["outline-panel"].hidden, true);

  assert.equal(elements.run.disabled, false);
  assert.equal(selectedRun(elements), undefined);

  await runItem(elements, 1).dispatch("click");
  assert.deepEqual(outlineLabels(elements), ["A plan"]);
});

test("selecting a run never calls a mutating endpoint", async () => {
  const runA = snapshot({runId: 1, state: "success", stdout: "A"});
  const runB = snapshot({
    runId: 2, state: "failed", stdout: "B",
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
        attempts: [],
      });
    },
  });

  // Viewing run #1: Run is blocked until an explicit New run.
  assert.equal(elements.run.disabled, true);

  await elements["new-run"].dispatch("click");
  elements["run-arguments"].value = "draft-arg";
  elements.code.value = "print('draft')";

  await elements.run.dispatch("click");

  assert.deepEqual(runRequestBody, {
    code: "print('draft')",
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
  assert.equal(elements["run-arguments"].value, "a");
  assert.equal(elements.code.value, "print('A')");

  delayedRun.resolve(response(runTwo));
  await new Promise((resolve) => setImmediate(resolve));

  assert.match(selectedRun(elements).textContent, /^#1/);
  assert.equal(elements["run-arguments"].value, "a");
  assert.equal(elements.code.value, "print('A')");
});

test("New run while already drafting never discards in-progress edits", async () => {
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {status: 200, body: {validation: []}},
  });

  elements["run-arguments"].value = "still-typing-arg";
  elements.code.value = "print('still typing')";

  await elements["new-run"].dispatch("click");

  assert.equal(elements["run-arguments"].value, "still-typing-arg");
  assert.equal(elements.code.value, "print('still typing')");
});

test("New run while drafting prevents a later SSE run from taking the selection", async () => {
  const runs = [];
  const details = {};
  const {elements, eventSource} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
  });

  elements.code.value = "print('explicit draft')";
  await elements["new-run"].dispatch("click");

  runs.push({runId: 9, state: "running", cwd: "/work/run-9"});
  details[9] = snapshot({runId: 9, state: "running", stdout: "elsewhere"});
  eventSource.emit("runner-change");
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(selectedRun(elements), undefined);
  assert.equal(elements.code.value, "print('explicit draft')");
});

test("New run while submitting invalidates the pending run selection", async () => {
  const runResponse = deferred();
  const {elements} = await loadApp({
    runs: [],
    details: {},
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url) {
      if (url === "/api/run") return runResponse.promise;
      return undefined;
    },
  });

  elements.code.value = "print('submitted')";
  const submission = elements.run.dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  elements.code.value = "print('newer draft')";
  await elements["new-run"].dispatch("click");

  runResponse.resolve(response(snapshot({
    runId: 1, state: "running", stdout: "", code: "print('submitted')",
  })));
  await submission;

  assert.equal(selectedRun(elements), undefined);
  assert.equal(elements.code.value, "print('newer draft')");
  assert.equal(elements.code.readOnly, false);
});

test("an SSE-discovered run cannot supersede a pending Run action", async () => {
  const runResponse = deferred();
  const runs = [];
  const details = {};
  const submitted = snapshot({
    runId: 2, state: "running", stdout: "submitted", code: "print('mine')",
  });
  const {elements, eventSource} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url) {
      if (url === "/api/run") return runResponse.promise;
      return undefined;
    },
  });

  elements.code.value = "print('mine')";
  const submission = elements.run.dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));

  runs.push({runId: 1, state: "running", cwd: "/work/external"});
  details[1] = snapshot({
    runId: 1, state: "running", stdout: "external", code: "print('external')",
  });
  eventSource.emit("runner-change");
  await waitFor(() => runItem(elements, 1) !== undefined);
  assert.equal(selectedRun(elements), undefined);

  runs.push({runId: 2, state: "running", cwd: submitted.cwd});
  details[2] = submitted;
  runResponse.resolve(response(submitted));
  await submission;

  assert.match(selectedRun(elements).textContent, /^#2/);
  assert.equal(elements.code.value, "print('mine')");
  assert.equal(elements.code.readOnly, true);
});

test("edits made while Run is pending remain in the retained draft", async () => {
  const runResponse = deferred();
  const runs = [];
  const details = {};
  const submitted = snapshot({
    runId: 1, state: "running", stdout: "", cwd: "/tmp/before",
    args: ["before"], code: "print('before')",
  });
  const {elements} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url) {
      if (url === "/api/run") return runResponse.promise;
      return undefined;
    },
  });

  elements["run-arguments"].value = "before";
  elements.code.value = "print('before')";
  const submission = elements.run.dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  elements["run-arguments"].value = "after";
  elements.code.value = "print('after')";

  runs.push({runId: 1, state: "running", cwd: submitted.cwd});
  details[1] = submitted;
  runResponse.resolve(response(submitted));
  await submission;
  await elements["new-run"].dispatch("click");

  assert.equal(elements["run-arguments"].value, "after");
  assert.equal(elements.code.value, "print('after')");
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

  await elements["new-run"].dispatch("click");

  assert.equal(elements["run-arguments"].value, "in-progress-arg");
  assert.equal(elements.code.value, "print('in progress')");
});

test("completed Workflow runs expose explicit Cleanup and retain their history", async () => {
  const retained = snapshot({
    runId: 1,
    state: "success",
    stdout: "done",
    resources: [{
      kind: "purplemux_tab",
      identity: "tab-1",
      metadata: {workspace_id: "ws-1"},
      cleanupState: "retained",
      cleanupError: null,
    }],
  });
  const cleaned = snapshot({
    ...retained,
    resources: [{...retained.resources[0], cleanupState: "cleaned"}],
    resourceCleanupStatus: "cleaned",
  });
  const runs = [{runId: 1, state: "success", cwd: retained.cwd}];
  const details = {1: retained};
  const {calls, elements} = await loadApp({
    runs,
    details,
    validation: {status: 200, body: {validation: []}},
    fetchOverride(url, options) {
      if (url === "/api/runs/1/cleanup" && options.method === "POST") {
        details[1] = cleaned;
        return response(cleaned);
      }
      return undefined;
    },
  });

  assert.equal(elements.cleanup.disabled, false);
  assert.match(elements["resources-summary"].textContent, /retained/);
  await elements.cleanup.dispatch("click");

  assert.ok(calls.some(([url, method]) => url === "/api/runs/1/cleanup" && method === "POST"));
  assert.equal(runs.length, 1);
  assert.equal(elements.cleanup.disabled, true);
  assert.match(elements["resources-summary"].textContent, /cleaned/);
});
