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
