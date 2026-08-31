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

async function write(clipboard, stdout, stderr) {
  await clipboard.writeText(formatOutput(stdout, stderr));
}

globalThis.runnerOutputClipboard = Object.freeze({formatOutput, write});
