"use strict";

function formatOutput(stdout, stderr) {
  const streams = [
    ["stdout", stdout],
    ["stderr", stderr],
  ].filter(([, content]) => content.length > 0);

  let output = "";
  for (const [name, content] of streams) {
    if (output.length > 0) {
      output += output.endsWith("\n") ? "\n" : "\n\n";
    }
    output += `${name}:\n${content}`;
  }
  return output;
}

async function write(clipboard, stdout, stderr, document = globalThis.document) {
  await writeText(formatOutput(stdout, stderr), clipboard, document);
}

async function writeText(text, clipboard, document) {
  if (clipboard && typeof clipboard.writeText === "function") {
    try {
      await clipboard.writeText(text);
      return;
    } catch {
      // The Clipboard API can be present but denied outside a secure context.
    }
  }

  if (!document || typeof document.execCommand !== "function") {
    throw new Error("Clipboard access is unavailable");
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  textarea.setSelectionRange(0, text.length);
  try {
    if (!document.execCommand("copy")) {
      throw new Error("Browser copy command failed");
    }
  } finally {
    document.body.removeChild(textarea);
  }
}

globalThis.runnerOutputClipboard = Object.freeze({formatOutput, write, writeText});
