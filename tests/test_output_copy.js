"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

require(path.join(
  __dirname,
  "..",
  "src",
  "purplemux_client",
  "web_static",
  "output-copy.js",
));

const {write} = globalThis.runnerOutputClipboard;

function recordingClipboard(error = null) {
  return {
    writes: [],
    async writeText(text) {
      this.writes.push(text);
      if (error !== null) throw error;
    },
  };
}

test("copies stdout-only output", async () => {
  const clipboard = recordingClipboard();

  await write(clipboard, "hello\n", "");

  assert.deepEqual(clipboard.writes, ["stdout:\nhello\n"]);
});

test("copies stderr-only output", async () => {
  const clipboard = recordingClipboard();

  await write(clipboard, "", "problem\n");

  assert.deepEqual(clipboard.writes, ["stderr:\nproblem\n"]);
});

test("labels and copies stdout and stderr", async () => {
  const clipboard = recordingClipboard();

  await write(clipboard, "hello\n", "problem\n");

  assert.deepEqual(clipboard.writes, [
    "stdout:\nhello\n\nstderr:\nproblem\n",
  ]);
});

test("copies empty output as an empty string", async () => {
  const clipboard = recordingClipboard();

  await write(clipboard, "", "");

  assert.deepEqual(clipboard.writes, [""]);
});

test("copies output collected so far while running", async () => {
  const snapshot = {state: "running", stdout: "partial", stderr: ""};
  const clipboard = recordingClipboard();

  await write(clipboard, snapshot.stdout, snapshot.stderr);

  assert.deepEqual(clipboard.writes, ["stdout:\npartial"]);
});

test("copies output in every completed state", async () => {
  for (const state of ["success", "failed", "stopped"]) {
    const snapshot = {state, stdout: "done\n", stderr: "note\n"};
    const clipboard = recordingClipboard();

    await write(clipboard, snapshot.stdout, snapshot.stderr);

    assert.deepEqual(clipboard.writes, [
      "stdout:\ndone\n\nstderr:\nnote\n",
    ]);
  }
});

test("reports clipboard failures without disabling later copies", async () => {
  const failure = new Error("permission denied");
  await assert.rejects(write(recordingClipboard(failure), "out", ""), failure);

  const clipboard = recordingClipboard();
  await write(clipboard, "next", "");
  assert.deepEqual(clipboard.writes, ["stdout:\nnext"]);
});
