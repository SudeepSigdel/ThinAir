// Pure formatting helpers (no DOM, no globals from app.js), so they can be tested under Node
// (tests/test_format_js.py). Loaded before app.js; plain script, no build step.
"use strict";

// 192 -> "3 min 12 s", 3725 -> "1 h 2 min". Anything that is not a sensible number of seconds
// gives "" so callers can simply leave it out.
function fmtDuration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "";
  const total = Math.round(seconds);
  if (total < 1) return "under a second";
  if (total < 60) return `${total} s`;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return m ? `${h} h ${m} min` : `${h} h`; // past an hour, seconds are noise
  return s ? `${m} min ${s} s` : `${m} min`;
}

if (typeof module !== "undefined" && module.exports) module.exports = { fmtDuration };
