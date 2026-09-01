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

const {write, writeText} = globalThis.runnerOutputClipboard;

function recordingClipboard(error = null) {
  return {
    writes: [],
    async writeText(text) {
      this.writes.push(text);
      if (error !== null) throw error;
    },
  };
}

function fallbackDocument({result = true, error = null} = {}) {
  const document = {
    appended: [],
    commands: [],
    body: {
      appendChild(element) {
        document.appended.push(element);
      },
      removeChild(element) {
        element.removed = true;
      },
    },
    createElement(tagName) {
      assert.equal(tagName, "textarea");
      return {
        attributes: {},
        removed: false,
        selected: false,
        selection: null,
        style: {},
        value: "",
        setAttribute(name, value) {
          this.attributes[name] = value;
        },
        select() {
          this.selected = true;
        },
        setSelectionRange(start, end) {
          this.selection = [start, end];
        },
      };
    },
    execCommand(command) {
      this.commands.push(command);
      if (error !== null) throw error;
      return result;
    },
  };
  return document;
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
  await assert.rejects(
    write(recordingClipboard(failure), "out", "", null),
    /Clipboard access is unavailable/,
  );

  const clipboard = recordingClipboard();
  await write(clipboard, "next", "");
  assert.deepEqual(clipboard.writes, ["stdout:\nnext"]);
});

test("falls back when the Clipboard API is unavailable", async () => {
  const document = fallbackDocument();

  await writeText("guide <text>\n", undefined, document);

  assert.equal(document.appended.length, 1);
  const textarea = document.appended[0];
  assert.equal(textarea.value, "guide <text>\n");
  assert.equal(textarea.attributes.readonly, "");
  assert.equal(textarea.selected, true);
  assert.deepEqual(textarea.selection, [0, 13]);
  assert.deepEqual(document.commands, ["copy"]);
  assert.equal(textarea.removed, true);
});

test("falls back when Clipboard API permission is denied", async () => {
  const clipboard = recordingClipboard(new Error("permission denied"));
  const document = fallbackDocument();

  await write(clipboard, "out", "err", document);

  assert.deepEqual(clipboard.writes, ["stdout:\nout\n\nstderr:\nerr"]);
  assert.equal(document.appended[0].value, "stdout:\nout\n\nstderr:\nerr");
});

test("reports fallback failure and always removes its textarea", async () => {
  const document = fallbackDocument({result: false});

  await assert.rejects(
    writeText("cannot copy", undefined, document),
    /Browser copy command failed/,
  );

  assert.equal(document.appended[0].removed, true);
});
