"""Integration tests: need a running Docker daemon (`pytest -m docker`)."""

import asyncio

import docker
import pytest

from thinair.sandbox import janitor
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxError, SandboxSpec, VolumeMount

IMAGE = "python:3.12-alpine"
pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def client():
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    try:
        c.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        c.images.pull(IMAGE)
    return c


@pytest.fixture
def mgr(client):
    yield SandboxManager(client)
    leftovers = client.containers.list(all=True, filters={"label": "thinair.managed=true"})
    assert leftovers == [], "a test leaked a container"


def sb(cmd: list[str], **kw) -> SandboxSpec:
    kw.setdefault("memory_bytes", 128 * 1024**2)
    kw.setdefault("timeout_s", 60)
    return SandboxSpec(image=IMAGE, command=cmd, job_id="t1", workdir="/tmp", **kw)  # noqa: S108


async def run(mgr, spec):
    lines: list[str] = []
    res = await mgr.run(spec, lines.append)
    await asyncio.sleep(0)  # flush call_soon_threadsafe callbacks
    return res, lines


async def test_output_and_exit_code(mgr) -> None:
    res, lines = await run(mgr, sb(["sh", "-c", "echo hello; echo oops >&2; exit 3"]))
    assert res.exit_code == 3 and not res.oom_killed and not res.timed_out
    assert "hello" in lines and "oops" in lines
    assert res.wall_s > 0


async def test_runs_as_non_root_without_capabilities(mgr) -> None:
    _, lines = await run(mgr, sb(["sh", "-c", "id -u; grep CapEff /proc/self/status"]))
    assert lines[0] == "10001"
    assert lines[1].split()[-1] == "0000000000000000"


async def test_root_filesystem_read_only(mgr) -> None:
    res, _ = await run(mgr, sb(["sh", "-c", "touch /nope"]))
    assert res.exit_code != 0


async def test_network_disabled_by_default(mgr) -> None:
    res, _ = await run(
        mgr, sb(["python", "-c", "import socket;socket.create_connection(('1.1.1.1',53),2)"])
    )
    assert res.exit_code != 0


async def test_oom_is_detected(mgr) -> None:
    res, _ = await run(
        mgr,
        sb(["python", "-c", "x = b'a' * (400 * 2**20); print(len(x))"], memory_bytes=64 * 1024**2),
    )
    assert res.oom_killed and res.exit_code == 137


async def test_timeout_kills(mgr) -> None:
    res, _ = await run(mgr, sb(["sleep", "30"], timeout_s=2))
    assert res.timed_out and res.wall_s < 15


async def test_cancel_removes_container(mgr, client) -> None:
    task = asyncio.create_task(mgr.run(sb(["sleep", "60"])))
    await asyncio.sleep(2)
    assert client.containers.list(filters={"label": "thinair.managed=true"})
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.containers.list(all=True, filters={"label": "thinair.managed=true"})


async def test_secret_redaction_and_stats(mgr) -> None:
    res, lines = await run(
        mgr,
        sb(
            ["python", "-c", "import os,time;print(os.environ['T']);time.sleep(2)"],
            env={"T": "s3cr3t-value"},
            redact=["s3cr3t-value"],
        ),
    )
    assert lines == ["***"]
    assert res.peak_mem_bytes and res.cpu_time_s is not None


async def test_named_volume_mounts(mgr) -> None:
    mgr.create_volume("thinair-test-vol", "t1")
    try:
        m = [VolumeMount(volume="thinair-test-vol", path="/data", read_only=True)]
        res, _ = await run(mgr, sb(["ls", "/data"], mounts=m))
        assert res.exit_code == 0
    finally:
        mgr.remove_volume("thinair-test-vol")


async def test_missing_image_gives_friendly_error(mgr) -> None:
    with pytest.raises(SandboxError, match="not found"):
        await mgr.run(SandboxSpec(image="thinair/does-not-exist:0", command=["x"], job_id="t1"))


def test_janitor_removes_orphans(client) -> None:
    client.containers.create(
        IMAGE, ["sleep", "1"], labels={"thinair.managed": "true", "thinair.job": "dead"}
    )
    assert janitor.sweep(client)["containers"] >= 1
    assert not client.containers.list(all=True, filters={"label": "thinair.managed=true"})


def test_janitor_spares_active_jobs(client) -> None:
    c = client.containers.create(
        IMAGE, ["sleep", "1"], labels={"thinair.managed": "true", "thinair.job": "live"}
    )
    try:
        janitor.sweep(client, keep_job_ids=frozenset({"live"}))
        assert client.containers.get(c.id)
    finally:
        c.remove(force=True)


async def test_cpu_request_above_the_host_is_capped_not_refused(mgr, client) -> None:
    """Docker itself refuses this ("range of CPUs is from 0.01 to N"); Thinair caps it."""
    res, _ = await run(mgr, sb(["true"], cpus=64))
    assert res.exit_code == 0
    assert mgr.host_cpus() == client.info()["NCPU"]
