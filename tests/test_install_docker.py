"""The installer's release-matching logic, run in a real busybox shell (what `curl | sh` meets on
a minimal host). Needs Docker (`pytest -m docker`)."""

import re
import subprocess
from pathlib import Path

import docker
import pytest

pytestmark = pytest.mark.docker
INSTALL = Path(__file__).resolve().parent.parent / "install.sh"
IMAGE = "alpine:3"


@pytest.fixture(scope="module")
def sh():
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    try:
        c.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        c.images.pull(IMAGE)
    text = INSTALL.read_text(encoding="utf-8")
    m = re.search(r"^release_tag\(\) \{.*?^\}\n", text, re.S | re.M)
    assert m, "install.sh has no release_tag function"
    func = m.group(0)

    def run(env: str = "", curl_url: str | None = None, curl_fails: bool = False) -> str:
        """`release_tag` with a stub `curl` that 'ends up' at curl_url; its stdout."""
        script = f"""set -eu
env_value() {{ printf ''; }}
curl() {{
  {"return 22" if curl_fails else f"printf '%s' '{curl_url or ''}'"}
}}
{func}
{env}
release_tag
"""
        r = subprocess.run(
            ["docker", "run", "--rm", "-i", IMAGE, "sh", "-s"],
            input=script.encode(),  # bytes: text mode would add a CR to every line on Windows
            capture_output=True,
            timeout=120,
        )
        assert r.returncode == 0, r.stderr.decode()
        return r.stdout.decode().strip()

    return run


LATEST = "https://github.com/SudeepSigdel/ThinAir/releases"


def test_a_pinned_version_uses_its_own_tag(sh) -> None:
    assert sh("THINAIR_VERSION=0.2.0") == "v0.2.0"


def test_latest_follows_the_latest_release_redirect(sh) -> None:
    assert sh("", curl_url=f"{LATEST}/tag/v0.3.1") == "v0.3.1"
    assert sh("THINAIR_VERSION=latest", curl_url=f"{LATEST}/tag/v1.0.0") == "v1.0.0"


def test_anything_unclear_falls_back_to_main(sh) -> None:
    assert sh("", curl_url=LATEST) == ""  # no release yet: /releases/latest ends at /releases
    assert sh("", curl_url=f"{LATEST}/tag/nightly") == ""  # not a version tag
    assert sh("", curl_fails=True) == ""  # offline, blocked, rate limited
    for v in ("edge", "local", "0.2", "abc"):
        assert sh(f"THINAIR_VERSION={v}", curl_url=f"{LATEST}/tag/v9.9.9") == "", v


def test_an_explicit_source_wins(sh) -> None:
    """THINAIR_RAW_URL (offline installs, tests) is never second-guessed."""
    assert sh("THINAIR_RAW_URL=file:///repo THINAIR_VERSION=0.2.0") == ""
