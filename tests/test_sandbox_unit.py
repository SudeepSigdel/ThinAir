import threading

import pytest
from pydantic import ValidationError

from thinair.sandbox.manager import build_container_args
from thinair.sandbox.spec import SandboxSpec, VolumeMount, parse_size


def spec(**kw) -> SandboxSpec:
    return SandboxSpec(image="img", command=["true"], job_id="j1", **kw)


def test_parse_size() -> None:
    assert parse_size("512m") == 512 * 1024**2
    assert parse_size("2g") == 2 * 1024**3
    assert parse_size(1000) == 1000
    with pytest.raises(ValueError):
        parse_size("lots")


def test_hardening_flags_always_applied() -> None:
    a = build_container_args(spec(cpus=0.5, memory_bytes=256 * 1024**2, pids_limit=64))
    assert a["user"] == "10001:10001"
    assert a["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in a["security_opt"]
    assert a["privileged"] is False
    assert a["read_only"] is True
    assert a["nano_cpus"] == 500_000_000
    assert a["mem_limit"] == a["memswap_limit"] == 256 * 1024**2  # swap disabled
    assert a["pids_limit"] == 64
    assert a["network_mode"] == "none"
    assert a["labels"]["thinair.managed"] == "true"


def test_network_opt_in() -> None:
    assert build_container_args(spec(network=True))["network_mode"] == "bridge"


@pytest.mark.parametrize(
    "volume", ["/var/run/docker.sock", "/etc", "../x", "C:\\Users", "a/b", "", "-bad"]
)
def test_only_named_volumes_can_be_mounted(volume: str) -> None:
    with pytest.raises(ValidationError):
        VolumeMount(volume=volume, path="/workspace")


@pytest.mark.parametrize("path", ["relative", "/var/run/docker.sock", "/a/../b"])
def test_bad_container_paths_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        VolumeMount(volume="vol", path=path)


def test_volumes_become_binds() -> None:
    a = build_container_args(
        spec(mounts=[VolumeMount(volume="ws", path="/workspace", read_only=True)])
    )
    assert a["volumes"] == {"ws": {"bind": "/workspace", "mode": "ro"}}


def test_bad_env_name_and_empty_command_rejected() -> None:
    with pytest.raises(ValidationError):
        spec(env={"BAD-NAME": "x"})
    with pytest.raises(ValidationError):
        SandboxSpec(image="i", command=[], job_id="j")


class _FakeClient:
    def __init__(self, ncpu=None, fail=False):
        self.calls = 0
        self.ncpu, self.fail = ncpu, fail

    def info(self):
        self.calls += 1
        if self.fail:
            from docker.errors import DockerException

            raise DockerException("down")
        return {"NCPU": self.ncpu}


def test_cpu_limits_are_capped_to_the_host() -> None:
    """Regression: Docker refuses --cpus above the host's count, so on a 1-CPU VM every job
    (installs and agents default to 2 CPUs) failed with 'Could not create sandbox'."""
    from thinair.sandbox.manager import SandboxManager

    client = _FakeClient(ncpu=1)
    mgr = SandboxManager(client)
    assert mgr.fit(spec(cpus=2)).cpus == 1.0
    assert mgr.fit(spec(cpus=0.5)).cpus == 0.5  # below the host: untouched
    assert mgr.fit(spec(cpus=1)).cpus == 1
    assert client.calls == 1  # asked once, then cached


def test_unknown_host_cpu_count_leaves_limits_alone() -> None:
    from thinair.sandbox.manager import SandboxManager

    for client in (_FakeClient(fail=True), _FakeClient(ncpu=None)):
        mgr = SandboxManager(client)
        assert mgr.host_cpus() == 0
        assert mgr.fit(spec(cpus=2)).cpus == 2


def test_terminal_specs() -> None:
    """Agent sign-ins get a TTY: only with stdin, same hardening, and never through run()."""
    with pytest.raises(ValidationError, match="stdin"):
        spec(tty=True)
    a = build_container_args(spec(tty=True, stdin=True))
    assert a["tty"] is True and a["stdin_open"] is True
    assert a["user"] == "10001:10001" and a["cap_drop"] == ["ALL"] and a["read_only"] is True
    assert build_container_args(spec())["tty"] is False

    from thinair.sandbox.manager import SandboxManager

    with pytest.raises(ValueError, match="open_terminal"):
        SandboxManager(client=object()).run_blocking(spec(tty=True, stdin=True))
    with pytest.raises(ValueError, match="tty=True"):
        SandboxManager(client=object()).open_terminal(spec(stdin=True))


# ---- output streaming: a line without a newline must not grow without bound ---------------------
def _stream(chunks, **spec_kw) -> tuple[list[str], threading.Event]:
    from thinair.sandbox import manager as m

    class Container:
        def logs(self, **_):
            return chunks

    out: list[str] = []
    truncated = threading.Event()
    m._stream_logs(Container(), spec(**spec_kw), out.append, truncated)
    return out, truncated


def test_output_without_newlines_uses_bounded_memory() -> None:
    """Regression (audit A1): `buf += chunk` kept the whole stream while no newline arrived, so a
    repo printing a progress bar or `yes | tr -d '\n'` grew the server's memory (and CPU,
    quadratically) without limit: 64 MB took 21 s and 134 MB. Now a long line is cut and the
    rest of it is dropped until the next newline."""
    import time
    import tracemalloc

    from thinair.sandbox import manager as m

    def chunks():
        block = b"A" * 65536
        for _ in range(512):  # 32 MiB, one endless line
            yield block
        yield b"\nnext line\n"

    tracemalloc.start()
    t0 = time.monotonic()
    out, _ = _stream(chunks())
    elapsed = time.monotonic() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 8 * 1024**2, f"peak {peak / 1e6:.0f} MB"
    assert elapsed < 3, f"{elapsed:.1f} s"
    assert out == ["A" * m.MAX_LINE_CHARS, "next line"]  # the start of the line, then normal again


def test_a_secret_inside_an_overlong_line_is_still_masked() -> None:
    secret = "s3cr3t-" + "k" * 600
    start = "x" * 7000 + secret  # visible part of the line (first MAX_LINE_CHARS), with the secret
    data = (start + "y" * 300_000 + "\nok\n").encode()
    chunks = [data[i : i + 4099] for i in range(0, len(data), 4099)]
    out, _ = _stream(chunks, redact=[secret])
    assert len(out) == 2 and out[1] == "ok"
    assert secret[:10] not in out[0] and "***" in out[0]


def test_normal_lines_unchanged_by_the_bound() -> None:
    out, _ = _stream(
        [b"one\r\ntw", b"o\n\nthree"]
    )  # CRLF, a line split across chunks, a blank, no final \n
    assert out == ["one", "two", "", "three"]
