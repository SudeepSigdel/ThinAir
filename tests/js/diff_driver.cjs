// Reads a unified diff on stdin, prints what the review screen's parser makes of it as JSON.
// Used by tests/test_diff_js.py (Node only; the test is skipped without it).
const path = require("path");
const { parseDiff, changeTargets } = require(path.join(__dirname, "../../src/thinair/web/static/diff.js"));
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => (input += c));
process.stdin.on("end", () => {
  const t0 = process.hrtime.bigint();
  const files = parseDiff(input);
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  const summary = process.argv.includes("--summary");
  const out = files.map((f) =>
    summary
      ? { ...f, hunks: f.hunks.map((h) => ({ header: h.header, lines: h.lines.length, firstChange: h.firstChange })) }
      : f,
  );
  process.stdout.write(JSON.stringify({ ms, targets: changeTargets(files), files: out }));
});
