"use strict";

function pad(number) {
  return String(number).padStart(2, "0");
}

function sameLocalDate(left, right) {
  return left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate();
}

function formatObservedAt(observedAt, now = new Date()) {
  const observed = new Date(observedAt);
  if (Number.isNaN(observed.getTime())) return "Unknown time";

  let dateLabel;
  if (sameLocalDate(observed, now)) {
    dateLabel = "Today";
  } else {
    const yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    dateLabel = sameLocalDate(observed, yesterday)
      ? "Yesterday"
      : `${observed.getMonth() + 1}/${observed.getDate()}`;
  }
  return `${dateLabel} ${pad(observed.getHours())}:${pad(observed.getMinutes())}:${pad(observed.getSeconds())}`;
}

function formatOutputEntries(entries, fallback = "") {
  if (!Array.isArray(entries) || entries.length === 0) return fallback;

  let output = "";
  let lineStarted = false;
  for (const entry of entries) {
    if (!entry || typeof entry.text !== "string" || entry.text.length === 0) continue;
    const label = formatObservedAt(entry.observedAt);
    let offset = 0;
    while (offset < entry.text.length) {
      if (!lineStarted) {
        output += `${label}  `;
        lineStarted = true;
      }
      const newline = entry.text.indexOf("\n", offset);
      if (newline === -1) {
        output += entry.text.slice(offset);
        break;
      }
      output += entry.text.slice(offset, newline + 1);
      lineStarted = false;
      offset = newline + 1;
    }
  }
  return output;
}

globalThis.runnerLogDisplay = Object.freeze({formatObservedAt, formatOutputEntries});
