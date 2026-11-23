"""The web app's pure formatting helpers (`web/static/format.js`), run under Node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
DRIVER = Path(__file__).parent / "js" / "format_driver.cjs"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not installed")


def durations(*seconds) -> list[str]:
    out = subprocess.run(
        [NODE, str(DRIVER)],
        input=json.dumps(list(seconds)).encode(),
        capture_output=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout)


def test_durations_read_like_a_person_would_say_them() -> None:
    assert durations(0.2, 1, 59.4, 59.6) == ["under a second", "1 s", "59 s", "1 min"]
    assert durations(60, 90, 192, 3599) == ["1 min", "1 min 30 s", "3 min 12 s", "59 min 59 s"]
    assert durations(3600, 3725, 7200, 7380) == ["1 h", "1 h 2 min", "2 h", "2 h 3 min"]
    assert durations(90000) == ["25 h"]  # no days: a job rarely runs that long, hours stay clear


def test_nonsense_gives_nothing_to_show() -> None:
    assert durations(-5, "NaN", None) == ["", "", ""]
