"""The review screen's diff parser (`web/static/diff.js`), run under Node. Skipped without Node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
DRIVER = Path(__file__).parent / "js" / "diff_driver.cjs"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not installed")


def parse(diff: str, *flags: str) -> dict:
    out = subprocess.run(
        [NODE, str(DRIVER), *flags],
        input=diff.encode("utf-8"),
        capture_output=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout)


def only(diff: str) -> dict:
    files = parse(diff)["files"]
    assert len(files) == 1
    return files[0]


BASIC = """\
diff --git a/src/dates.py b/src/dates.py
index 1111111..2222222 100644
--- a/src/dates.py
+++ b/src/dates.py
@@ -10,4 +10,5 @@ def parse_iso(s):
 def test_parse_iso():
-    d = parse("2024-01-01")
+    d = parse("2024-01-01",
+              tz=UTC)
     assert d.year == 2024
@@ -40,3 +41,3 @@ class Thing:
     a = 1
-    b = 2
+    b = 3
     c = 4
"""


def test_a_normal_diff_gives_files_hunks_and_line_numbers() -> None:
    f = only(BASIC)
    assert (f["path"], f["status"], f["additions"], f["deletions"]) == (
        "src/dates.py",
        "modified",
        3,
        2,
    )
    h1, h2 = f["hunks"]
    assert h1["context"] == "def parse_iso(s):" and h1["firstChange"] == 1
    rows = [(ln["t"], ln["o"], ln["n"], ln["s"]) for ln in h1["lines"]]
    assert rows[0] == (" ", 10, 10, "def test_parse_iso():")
    assert rows[1] == ("-", 11, None, '    d = parse("2024-01-01")')  # removed: the old number only
    assert rows[2] == ("+", None, 11, '    d = parse("2024-01-01",')  # added: the new number only
    assert rows[3] == ("+", None, 12, "              tz=UTC)")
    assert rows[4] == (" ", 12, 13, "    assert d.year == 2024")
    assert h2["lines"][0]["o"] == 40 and h2["lines"][0]["n"] == 41  # the second hunk renumbers
    assert f["lineCount"] == 5 + 4


def test_targets_are_one_per_hunk_across_files() -> None:
    both = BASIC + BASIC.replace("src/dates.py", "src/other.py")
    r = parse(both)
    assert r["targets"] == [
        {"file": 0, "hunk": 0},
        {"file": 0, "hunk": 1},
        {"file": 1, "hunk": 0},
        {"file": 1, "hunk": 1},
    ]


def test_crlf_lines_are_flagged_not_mangled() -> None:
    diff = (
        "diff --git a/w.txt b/w.txt\n--- a/w.txt\n+++ b/w.txt\n@@ -1,2 +1,2 @@\n"
        " keep\r\n-old line\r\n+new line\r\n"
    )
    h = only(diff)["hunks"][0]["lines"]
    assert [(ln["t"], ln["s"], ln["cr"]) for ln in h] == [
        (" ", "keep", True),
        ("-", "old line", True),
        ("+", "new line", True),
    ]
    mixed = only("diff --git a/m b/m\n--- a/m\n+++ b/m\n@@ -1 +1 @@\n-same\n+same\r\n")["hunks"][0][
        "lines"
    ]
    assert [ln["cr"] for ln in mixed] == [False, True]  # a line-ending change is visible


def test_binary_files_have_no_hunks() -> None:
    f = only(
        "diff --git a/logo.png b/logo.png\nindex 1..2 100644\nBinary files a/logo.png and b/logo.png differ\n"
    )
    assert f["binary"] is True and f["hunks"] == [] and f["path"] == "logo.png"
    g = only(
        "diff --git a/x.bin b/x.bin\nnew file mode 100644\nindex 0..2\nGIT binary patch\nliteral 3\nHcmZ\n\n"
    )
    assert g["binary"] is True and g["status"] == "added"


def test_renames_with_and_without_edits() -> None:
    pure = only(
        "diff --git a/old name.txt b/new name.txt\nsimilarity index 100%\nrename from old name.txt\nrename to new name.txt\n"
    )
    assert (pure["status"], pure["oldPath"], pure["path"], pure["hunks"]) == (
        "renamed",
        "old name.txt",
        "new name.txt",
        [],
    )
    edited = only(
        "diff --git a/a.py b/b.py\nsimilarity index 80%\nrename from a.py\nrename to b.py\n"
        "index 1..2 100644\n--- a/a.py\n+++ b/b.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    )
    assert (edited["status"], edited["oldPath"], edited["path"]) == ("renamed", "a.py", "b.py")
    assert (edited["additions"], edited["deletions"]) == (1, 1)


def test_no_newline_at_end_of_file_marks_the_line_before_it() -> None:
    f = only(
        "diff --git a/n b/n\n--- a/n\n+++ b/n\n@@ -1 +1 @@\n-last\n\\ No newline at end of file\n+last\n"
    )
    lines = f["hunks"][0]["lines"]
    assert [(ln["t"], ln["noEol"]) for ln in lines] == [("-", True), ("+", False)]
    assert f["lineCount"] == 2  # the marker is not a line


def test_added_and_deleted_files() -> None:
    added = only(
        "diff --git a/n.md b/n.md\nnew file mode 100644\nindex 0..1\n--- /dev/null\n+++ b/n.md\n@@ -0,0 +1,2 @@\n+one\n+two\n"
    )
    assert (added["status"], added["additions"], added["path"]) == ("added", 2, "n.md")
    assert added["hunks"][0]["lines"][0]["n"] == 1
    gone = only(
        "diff --git a/d.md b/d.md\ndeleted file mode 100644\nindex 1..0\n--- a/d.md\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n"
    )
    assert (gone["status"], gone["deletions"], gone["path"]) == ("deleted", 2, "d.md")


def test_paths_with_spaces_and_quoted_unicode() -> None:
    assert (
        only(
            "diff --git a/my docs/read me.md b/my docs/read me.md\n--- a/my docs/read me.md\n+++ b/my docs/read me.md\n@@ -1 +1 @@\n-a\n+b\n"
        )["path"]
        == "my docs/read me.md"
    )
    quoted = only(
        'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n--- "a/caf\\303\\251.txt"\n+++ "b/caf\\303\\251.txt"\n@@ -1 +1 @@\n-a\n+b\n'
    )
    assert quoted["path"] == "café.txt"


def test_removed_lines_that_look_like_headers_are_still_lines() -> None:
    # Deleting a markdown rule ("---") and a "-- sql comment" must not start a new file or hunk.
    f = only(
        "diff --git a/d.md b/d.md\n--- a/d.md\n+++ b/d.md\n@@ -1,4 +1,2 @@\n"
        " title\n----\n--- sql comment\n+++ plus plus\n tail\n"
    )
    rows = [(ln["t"], ln["s"]) for ln in f["hunks"][0]["lines"]]
    assert rows == [
        (" ", "title"),
        ("-", "---"),
        ("-", "-- sql comment"),
        ("+", "++ plus plus"),
        (" ", "tail"),
    ]
    assert (f["additions"], f["deletions"]) == (1, 2)


def test_a_truncated_or_odd_diff_never_crashes() -> None:
    cut = BASIC[: BASIC.index("tz=UTC") + 4]
    f = only(cut)  # ends mid-hunk
    assert f["hunks"][0]["lines"]
    assert parse("")["files"] == [] and parse("\n\n")["files"] == []
    assert parse("not a diff at all\njust text\n")["files"] == []
    assert (
        only("diff --git a/x b/x\n@@ garbage @@\n+x\n")["hunks"] == []
    )  # a bad hunk header is skipped


def test_a_five_thousand_line_diff_parses_fast() -> None:
    body = "".join(f"+line {i}\n" for i in range(5000))
    diff = (
        "diff --git a/big.txt b/big.txt\nnew file mode 100644\n--- /dev/null\n+++ b/big.txt\n@@ -0,0 +1,5000 @@\n"
        + body
    )
    r = parse(diff, "--summary")
    f = r["files"][0]
    assert f["additions"] == 5000 and f["hunks"][0]["lines"] == 5000 and f["lineCount"] == 5000
    assert r["ms"] < 500  # a single pass; no per-line regexes


def test_many_files_in_one_diff() -> None:
    diff = "".join(BASIC.replace("src/dates.py", f"src/f{i}.py") for i in range(50))
    files = parse(diff, "--summary")["files"]
    assert [f["path"] for f in files] == [f"src/f{i}.py" for i in range(50)]
