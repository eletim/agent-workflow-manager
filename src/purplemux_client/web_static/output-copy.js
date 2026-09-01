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

  const previousFocus = document.activeElement;
  const selection = typeof document.getSelection === "function"
    ? document.getSelection()
    : null;
  const previousRanges = [];
  if (selection) {
    for (let index = 0; index < selection.rangeCount; index += 1) {
      previousRanges.push(selection.getRangeAt(index).cloneRange());
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.position = "fixed";
  textarea.style.top = "0";
  textarea.style.left = "-9999px";
  // Elements outside an open modal dialog are inert. Keep the controlled
  // selection inside that dialog so execCommand copies the requested payload.
  const selectionHost = previousFocus?.closest?.("dialog[open]") || document.body;
  selectionHost.appendChild(textarea);
  textarea.focus({preventScroll: true});
  textarea.select();
  textarea.setSelectionRange(0, text.length);
  try {
    if (!document.execCommand("copy")) {
      throw new Error("Browser copy command failed");
    }
  } finally {
    selectionHost.removeChild(textarea);
    previousFocus?.focus?.({preventScroll: true});
    if (selection) {
      selection.removeAllRanges();
      for (const range of previousRanges) selection.addRange(range);
    }
  }
}

globalThis.runnerOutputClipboard = Object.freeze({formatOutput, write, writeText});
