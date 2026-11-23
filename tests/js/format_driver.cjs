// Reads a JSON array of seconds on stdin, prints fmtDuration of each as a JSON array.
// Used by tests/test_format_js.py (Node only; the test is skipped without it).
const path = require("path");
const { fmtDuration } = require(path.join(__dirname, "../../src/thinair/web/static/format.js"));
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => (input += c));
process.stdin.on("end", () => {
  const values = JSON.parse(input).map((v) => (v === "NaN" ? NaN : v));
  process.stdout.write(JSON.stringify(values.map(fmtDuration)));
});
