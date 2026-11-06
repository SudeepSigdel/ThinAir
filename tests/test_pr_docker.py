"""The PR pipeline with real containers, minus GitHub: an agent's exact patch is captured from
its workspace, then applied and committed in a fresh workspace, byte for byte."""

from pathlib import Path

import docker
import pytest

from thinair.agents.cli_adapter import CLIAdapter
from thinair.agents.config import AgentConfig
from thinair.jobs import pr
from thinair.project.prepare import DirSource, dir_to_tar, prepare_workspace, workspace_volume
from thinair.sandbox.manager import SandboxManager

FIXTURE = Path(__file__).parent / "fixtures" / "node_app"
pytestmark = pytest.mark.docker
JOB = "prtest"

# Awkward changes on purpose: CRLF text, a binary file, a deletion, an edit to a file that the
# install step created (not part of the cloned commit), plus a normal edit.
SCRIPT = r"""
printf 'one\r\ntwo\r\n' > crlf.txt
printf '\000\001\002\377binary' > bin.dat
rm package.json
echo "more" >> gen.lock
echo "// edited" >> sum.js
printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"result":"Done."}'
"""


def agent() -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "id": "fakeagent",
            "name": "Fake Agent",
            "install": "true",
            "output": "stream-json",
            "command": ["sh", "-c", SCRIPT, "fake", "{task}"],
            "auth": {"login_command": ["sh"], "env_passthrough": ["FAKE_TOKEN"]},
            "limits": {"cpus": 1, "memory": "512m", "pids": 256, "timeout_s": 60},
        }
    )


@pytest.fixture(scope="module")
def mgr():
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    return SandboxManager(c)


@pytest.fixture
def volumes(mgr):
    made: list[str] = []
    yield made
    for v in made:
        mgr.remove_volume(v)
    mgr.remove_volume("thinair-auth-fakeagent")
    label = {"label": "thinair.managed=true"}
    assert not mgr.client.containers.list(all=True, filters=label), "container leaked"


async def git(mgr, volume, *args) -> list[str]:
    ident = ["-c", "user.name=T", "-c", "user.email=t@t"]
    code, out = await pr._git(mgr, JOB, volume, [*ident, *args], lambda _: None)
    assert code == 0, out
    return out


async def test_agent_patch_applies_to_a_fresh_copy_byte_for_byte(mgr, volumes, monkeypatch):
    from thinair.config import get_settings

    monkeypatch.setenv("THINAIR_GIT_AUTHOR_NAME", "Pat Owner")
    monkeypatch.setenv("THINAIR_GIT_AUTHOR_EMAIL", "pat@example.com")
    get_settings.cache_clear()

    prepared = await prepare_workspace(mgr, DirSource(FIXTURE), JOB, lambda _: None)
    volumes.append(prepared.volume)
    # Simulate a clone: the project files are a commit, then "install" leaves an untracked file.
    await git(mgr, prepared.volume, "init", "-q")
    await git(mgr, prepared.volume, "add", "--", "package.json", "sum.js", "test")
    await git(mgr, prepared.volume, "commit", "-q", "-m", "upstream")
    sha = (await git(mgr, prepared.volume, "rev-parse", "HEAD"))[0]
    prepared.commit_sha = sha
    res, out = await mgr.run_capture(
        pr.SandboxSpec(
            image="thinair-sandbox-python:2",
            command=["sh", "-c", "echo generated > gen.lock"],
            job_id=JOB,
            mounts=[pr.VolumeMount(volume=prepared.volume, path="/workspace")],
        )
    )
    assert res.exit_code == 0, out

    adapter = CLIAdapter(agent(), mgr, JOB, prepared, host_env={"FAKE_TOKEN": "x" * 12})
    await adapter.prepare(lambda _: None)
    _ = [ev async for ev in adapter.run("make awkward changes")]
    r = adapter.result()
    assert r.success, r.logs_tail
    assert r.patch is not None and not r.patch_too_large
    assert b"GIT binary patch" in r.patch and b"one\r\n" in r.patch
    assert b"node_modules" not in r.patch

    # A fresh workspace holding only the upstream commit (what a PR job fetches).
    fresh = workspace_volume(JOB + "b")
    volumes.append(fresh)
    mgr.create_volume(fresh, JOB)
    mgr.put_tar(fresh, pr.GIT_IMAGE, dir_to_tar(FIXTURE), JOB)
    await git(mgr, fresh, "init", "-q")
    await git(mgr, fresh, "add", "-A")
    await git(mgr, fresh, "commit", "-q", "-m", "upstream")

    logs: list[str] = []
    await pr.build_commit(
        mgr,
        JOB,
        fresh,
        url=None,
        sha=sha,
        patch=r.patch,
        message=("Make awkward changes", "Opened from Thinair job x."),
        log=logs.append,
        token=None,
    )

    def read(name: str) -> bytes | None:
        return mgr.get_file(fresh, f"/workspace/{name}", pr.GIT_IMAGE, JOB, 1024**2)

    assert read("crlf.txt") == b"one\r\ntwo\r\n"
    assert read("bin.dat") == b"\x00\x01\x02\xffbinary"
    assert read("gen.lock") == b"generated\nmore\n"  # install output the agent edited: kept
    assert read("package.json") is None  # deleted
    assert read("sum.js").endswith(b"// edited\n")
    # Committed as the configured author, with the message as given (no extra trailers).
    log_out = await git(mgr, fresh, "log", "-1", "--format=%an <%ae>|%B")
    assert log_out[0] == "Pat Owner <pat@example.com>|Make awkward changes"
    assert not [ln for ln in log_out if "co-authored" in ln.lower()]
    status = await git(mgr, fresh, "status", "--porcelain")
    assert status == [], status  # everything the patch touched is committed


async def test_patch_that_no_longer_applies_gives_a_clear_error(mgr, volumes) -> None:
    fresh = workspace_volume(JOB + "c")
    volumes.append(fresh)
    mgr.create_volume(fresh, JOB)
    mgr.put_tar(fresh, pr.GIT_IMAGE, dir_to_tar(FIXTURE), JOB)
    await git(mgr, fresh, "init", "-q")
    await git(mgr, fresh, "add", "-A")
    await git(mgr, fresh, "commit", "-q", "-m", "upstream")
    bad = b"diff --git a/nope.txt b/nope.txt\n--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(pr.PRError) as e:
        await pr.build_commit(
            mgr,
            JOB,
            fresh,
            url=None,
            sha="0" * 40,
            patch=bad,
            message=("t", "b"),
            log=lambda _: None,
            token=None,
        )
    assert e.value.code == "patch_failed"


async def test_push_with_a_rejected_token_explains_the_permission(mgr, volumes) -> None:
    """Needs github.com. A fake token: GitHub refuses, and the message names the permission."""
    fresh = workspace_volume(JOB + "d")
    volumes.append(fresh)
    mgr.create_volume(fresh, JOB)
    await git(mgr, fresh, "init", "-q")
    await git(mgr, fresh, "remote", "add", "origin", "https://github.com/octocat/Hello-World")
    await git(mgr, fresh, "commit", "-q", "--allow-empty", "-m", "x")
    token = "github_pat_" + "Qq7" * 20
    logs: list[str] = []
    with pytest.raises(pr.PRError) as e:
        await pr.push_branch(mgr, JOB, fresh, "thinair/test", token, logs.append)
    assert e.value.code == "push_failed" and "Contents: Read and write" in e.value.message
    assert not [ln for ln in [e.value.message, *logs] if token in ln]
