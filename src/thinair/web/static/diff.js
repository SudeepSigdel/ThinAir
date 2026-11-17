// Unified-diff parser for the review screen. Pure (no DOM), so it runs under Node in the tests.
// Input: the text of `git diff` for the agent's change. Output: files -> hunks -> lines.
//
// Hunk bodies are consumed by the counts in their `@@ -a,b +c,d @@` header, so a removed line whose
// text starts with "-- " is never mistaken for a file header. The parser is tolerant: a truncated
// diff simply ends mid-hunk, and unknown header lines are ignored.
"use strict";

function unquoteGitPath(p) {
  if (p.length >= 2 && p.startsWith('"') && p.endsWith('"')) {
    const inner = p.slice(1, -1);
    const bytes = [];
    for (let i = 0; i < inner.length; i++) {
      const c = inner[i];
      if (c !== "\\") {
        for (const b of new TextEncoder().encode(c)) bytes.push(b);
        continue;
      }
      const n = inner[++i];
      if (n >= "0" && n <= "7") {
        let oct = n;
        while (oct.length < 3 && inner[i + 1] >= "0" && inner[i + 1] <= "7") oct += inner[++i];
        bytes.push(parseInt(oct, 8));
      } else {
        bytes.push(({ t: 9, n: 10, r: 13, '"': 34, "\\": 92 })[n] ?? n.charCodeAt(0));
      }
    }
    return new TextDecoder().decode(new Uint8Array(bytes));
  }
  return p;
}

function stripPrefix(p) {
  p = unquoteGitPath(p.trim());
  return /^[ab]\//.test(p) ? p.slice(2) : p;
}

function pathsFromHeader(line) {
  // "diff --git a/X b/Y": X and Y are equal unless renamed, and may contain spaces.
  const rest = line.slice("diff --git ".length);
  if (rest.startsWith('"')) {
    const m = rest.match(/^("(?:[^"\\]|\\.)*")\s+("(?:[^"\\]|\\.)*")$/);
    if (m) return [stripPrefix(m[1]), stripPrefix(m[2])];
  }
  const n = (rest.length - 5) / 2;
  if (Number.isInteger(n) && n > 0 && rest.startsWith("a/") && rest.slice(2, 2 + n) === rest.slice(5 + n)) {
    return [rest.slice(2, 2 + n), rest.slice(5 + n)];
  }
  const m = rest.match(/^a\/(.+) b\/(.+)$/);
  return m ? [m[1], m[2]] : [rest, rest];
}

function parseHunkHeader(line) {
  const m = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/);
  if (!m) return null;
  return {
    oldStart: Number(m[1]),
    oldCount: m[2] === undefined ? 1 : Number(m[2]),
    newStart: Number(m[3]),
    newCount: m[4] === undefined ? 1 : Number(m[4]),
    context: m[5] || "",
  };
}

function parseDiff(text) {
  const files = [];
  if (!text) return files;
  const lines = text.split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop(); // the final newline
  let file = null;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("diff --git ")) {
      const [oldPath, newPath] = pathsFromHeader(line);
      file = { path: newPath, oldPath, status: "modified", binary: false, hunks: [], additions: 0, deletions: 0, lineCount: 0, notes: [] };
      files.push(file);
      i++;
      continue;
    }
    if (!file) {
      i++; // anything before the first file
      continue;
    }
    if (line.startsWith("@@")) {
      const h = parseHunkHeader(line);
      if (!h) {
        i++;
        continue;
      }
      const hunk = { header: line, context: h.context, oldStart: h.oldStart, newStart: h.newStart, lines: [], firstChange: -1 };
      let o = h.oldStart;
      let n = h.newStart;
      let remOld = h.oldCount;
      let remNew = h.newCount;
      i++;
      while (i < lines.length && (remOld > 0 || remNew > 0 || lines[i].startsWith("\\"))) {
        const body = lines[i];
        const c = body[0];
        if (c === "\\") {
          const prev = hunk.lines[hunk.lines.length - 1];
          if (prev) prev.noEol = true;
          i++;
          continue;
        }
        let t;
        if (c === "+") t = "+";
        else if (c === "-") t = "-";
        else if (c === " " || body === "") t = " "; // a blank context line may have lost its space
        else break; // not part of this hunk: a damaged or truncated diff
        let s = body.slice(1);
        const cr = s.endsWith("\r");
        if (cr) s = s.slice(0, -1);
        const ln = { t, o: t === "+" ? null : o, n: t === "-" ? null : n, s, cr, noEol: false };
        if (t !== "+") {
          o++;
          remOld--;
        }
        if (t !== "-") {
          n++;
          remNew--;
        }
        if (t === "+") file.additions++;
        else if (t === "-") file.deletions++;
        if (t !== " " && hunk.firstChange < 0) hunk.firstChange = hunk.lines.length;
        hunk.lines.push(ln);
        i++;
      }
      file.lineCount += hunk.lines.length;
      file.hunks.push(hunk);
      continue;
    }
    if (line.startsWith("new file mode")) file.status = "added";
    else if (line.startsWith("deleted file mode")) file.status = "deleted";
    else if (line.startsWith("rename from ")) {
      file.status = "renamed";
      file.oldPath = unquoteGitPath(line.slice("rename from ".length));
    } else if (line.startsWith("rename to ")) file.path = unquoteGitPath(line.slice("rename to ".length));
    else if (line.startsWith("Binary files ") || line.startsWith("GIT binary patch")) file.binary = true;
    else if (line.startsWith("old mode ") || line.startsWith("new mode ")) file.notes.push(line);
    else if (line.startsWith("--- ") && file.hunks.length === 0) {
      const p = line.slice(4).split("\t")[0];
      if (p === "/dev/null") file.status = "added";
    } else if (line.startsWith("+++ ") && file.hunks.length === 0) {
      const p = line.slice(4).split("\t")[0];
      if (p === "/dev/null") file.status = "deleted";
      else if (file.status !== "renamed") file.path = stripPrefix(p);
    }
    i++;
  }
  return files;
}

// Every place a person would want to jump to: one per hunk that changes something.
function changeTargets(files) {
  const out = [];
  files.forEach((f, fi) => f.hunks.forEach((h, hi) => out.push({ file: fi, hunk: hi })));
  if (!out.length) files.forEach((f, fi) => out.push({ file: fi, hunk: -1 })); // binary or mode-only
  return out;
}

if (typeof module !== "undefined" && module.exports) module.exports = { parseDiff, changeTargets };
