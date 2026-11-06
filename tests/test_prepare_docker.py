"""Integration tests for the prepare pipeline (need Docker + internet for pip/npm)."""

from pathlib import Path

import docker
import pytest

from thinair.project.prepare import DirSource, GitSource, PrepareError, prepare_workspace
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxSpec, VolumeMount

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def mgr():
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    return SandboxManager(c)


@pytest.fixture
def cleanup(mgr):
    vols: list[str] = []
    yield vols
    for v in vols:
        mgr.remove_volume(v)
    left = mgr.client.volumes.list(filters={"label": "thinair.managed=true"})
    assert not [v for v in left if v.name.startswith("thinair-ws-t5")], "workspace volume leaked"


async def run_tests(mgr, prepared, memory_bytes: int):
    r = prepared.resolved
    return await mgr.run_capture(
        SandboxSpec(
            image=r.image,
            command=["sh", "-c", r.test],
            job_id="t5",
            workdir=r.workdir,
            env=r.env,
            mounts=[VolumeMount(volume=prepared.volume, path="/workspace")],
            memory_bytes=memory_bytes,
            cpus=1,
            timeout_s=120,
            network=False,
        )
    )


@pytest.mark.parametrize(("fixture", "stack"), [("py_fastapi_app", "python"), ("node_app", "node")])
async def test_fixture_prepares_and_has_known_memory_floor(mgr, cleanup, fixture, stack) -> None:
    logs: list[str] = []
    job = f"t5{fixture[:3]}"
    prepared = await prepare_workspace(mgr, DirSource(FIXTURES / fixture), job, logs.append)
    cleanup.append(prepared.volume)
    assert prepared.resolved.stack == stack

    ok, out = await run_tests(mgr, prepared, 1024**3)
    assert ok.exit_code == 0 and not ok.oom_killed, "\n".join(out[-20:])

    low, _ = await run_tests(mgr, prepared, 64 * 1024**2)  # far below the ~150 MB tests hold
    assert low.oom_killed or low.exit_code != 0


async def test_invalid_thinair_yaml_is_reported_cleanly(mgr, cleanup, tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "thinair.yaml").write_text("tset: oops\n")
    with pytest.raises(PrepareError) as e:
        await prepare_workspace(mgr, DirSource(tmp_path), "t5cfg", lambda _: None)
    assert e.value.code == "config_invalid" and "tset" in e.value.message


async def test_failing_install_gives_actionable_error_and_cleans_up(mgr, cleanup, tmp_path) -> None:
    (tmp_path / "thinair.yaml").write_text("stack: python\ninstall: exit 7\n")
    with pytest.raises(PrepareError) as e:
        await prepare_workspace(mgr, DirSource(tmp_path), "t5ins", lambda _: None)
    assert e.value.code == "install_failed" and "code 7" in e.value.message
    # the cleanup fixture asserts that no workspace volume remains


FAKE_TOKEN = "github_pat_" + "Zz9" * 20


async def test_git_image_reads_the_token_from_env_and_output_is_masked(mgr, cleanup) -> None:
    """The token reaches git only through GIT_CONFIG_* env (needs git >= 2.31 in the image)."""
    from thinair.github import git_auth_env
    from thinair.project.prepare import GIT_IMAGE, ensure_image

    await ensure_image(mgr, "git", lambda _: None)
    env, redact = git_auth_env(FAKE_TOKEN)
    res, lines = await mgr.run_capture(
        SandboxSpec(
            image=GIT_IMAGE,
            command=["config", "--get", "http.https://github.com/.extraheader"],
            job_id="t5tok",
            env={"HOME": "/tmp", **env},
            redact=redact,
            timeout_s=30,
        )
    )
    assert res.exit_code == 0, lines
    assert lines == ["Authorization: Basic ***"]


async def test_rejected_token_explains_why_and_never_leaks(mgr, cleanup, monkeypatch) -> None:
    """GitHub refuses a bad token even for a public repo; the error must point at the token."""
    from thinair.config import get_settings

    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", FAKE_TOKEN)
    get_settings.cache_clear()
    logs: list[str] = []
    with pytest.raises(PrepareError) as e:
        await prepare_workspace(
            mgr, GitSource("https://github.com/octocat/Hello-World"), "t5badtok", logs.append
        )
    assert e.value.code == "clone_failed", e.value.message
    assert "THINAIR_GITHUB_TOKEN" in e.value.message and "expired" in e.value.message
    assert not [ln for ln in [e.value.message, *logs] if FAKE_TOKEN in ln]


async def test_clone_of_missing_repo_explains_why(mgr, cleanup, monkeypatch) -> None:
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "")  # a developer's .env must not change this
    url = "https://github.com/thinair-nonexistent/does-not-exist-xyz"
    with pytest.raises(PrepareError) as e:
        await prepare_workspace(mgr, GitSource(url), "t5git", lambda _: None)
    assert e.value.code == "clone_failed" and "private" in e.value.message
    # Regression: this used to fail with "Permission denied" (root-owned volume) for *every* repo,
    # which the generic message above hid. The real reason must be about the repo, not our setup.
    assert "Permission denied" not in e.value.message


async def test_bad_url_never_touches_docker(mgr) -> None:
    with pytest.raises(PrepareError) as e:
        await prepare_workspace(mgr, GitSource("file:///etc"), "t5bad", lambda _: None)
    assert e.value.code == "bad_repo_url"


async def test_real_public_repo_is_cloned_and_reaches_stack_detection(mgr, cleanup) -> None:
    """Needs github.com. octocat/Hello-World is GitHub's tiny canonical test repository."""
    from thinair.project.prepare import _list_names, _load_source, ensure_image, workspace_volume

    url = "https://github.com/octocat/Hello-World"
    volume = workspace_volume("t5real")
    cleanup.append(volume)
    mgr.create_volume(volume, "t5real")
    await ensure_image(mgr, "python", lambda _: None)
    logs: list[str] = []
    commit = await _load_source(mgr, GitSource(url), "t5real", volume, logs.append)
    assert commit and len(commit) == 40 and all(c in "0123456789abcdef" for c in commit), logs
    assert "README" in await _list_names(mgr, "t5real", volume)

    # And through the public entry point: the clone works, and the repo (no code) is then
    # rejected with the advice to add a thinair.yaml, not with a clone or permission error.
    with pytest.raises(PrepareError) as e:
        await prepare_workspace(mgr, GitSource(url), "t5real2", lambda _: None)
    assert e.value.code == "config_invalid" and "thinair.yaml" in e.value.message


@pytest.mark.parametrize("key", ["python", "node", "git"])
async def test_every_workspace_image_hands_the_volume_to_the_sandbox_user(
    mgr, cleanup, key
) -> None:
    """Regression: Docker re-applies an image's /workspace ownership to an empty volume each time a
    container is created, so an image whose /workspace is not owned by 10001 breaks later writes."""
    from thinair.project.prepare import BUILDABLE_IMAGES, ensure_image, workspace_volume
    from thinair.sandbox.spec import SandboxSpec, VolumeMount

    image = await ensure_image(mgr, key, lambda _: None)
    assert image == BUILDABLE_IMAGES[key]
    volume = workspace_volume(f"t5own{key}")
    cleanup.append(volume)
    mgr.create_volume(volume, f"t5own{key}")
    spec = SandboxSpec(
        image=image,
        # git image: the entrypoint is `git`, so use its own file-writing command
        command=["init", "-q", "."] if key == "git" else ["sh", "-c", "mkdir d && touch d/f"],
        job_id=f"t5own{key}",
        mounts=[VolumeMount(volume=volume, path="/workspace")],
        env={"HOME": "/tmp"},
        timeout_s=30,
    )
    res, lines = await mgr.run_capture(spec)
    assert res.exit_code == 0, lines


@pytest.mark.parametrize("stack", ["python", "node"])
async def test_dependency_cache_is_writable_and_survives_jobs(mgr, stack) -> None:
    """The install step sees the per-project cache on /cache (writable by the sandbox user, even
    as a fresh empty volume) with the package managers pointed at it; it outlives the job."""
    from thinair.project.config import parse_project_config
    from thinair.project.detect import resolve_project
    from thinair.project.prepare import CACHE_LABEL, _install, cache_volume, ensure_image

    cache = cache_volume(f"https://github.com/thinair-test/{stack}", "t5cache")
    names = {"requirements.txt"} if stack == "python" else {"package.json"}
    base = resolve_project(names, parse_project_config(None))
    await ensure_image(mgr, stack, lambda _: None)
    mgr.ensure_persistent_volume(cache, {CACHE_LABEL: "true"})
    try:
        for i, cmd in enumerate(
            [
                'mkdir -p "$PIP_CACHE_DIR" "$npm_config_cache" && echo kept > "$PIP_CACHE_DIR/m"',
                'test "$(cat /cache/pip/m)" = kept && test -d /cache/npm && test -w /cache',
            ]
        ):
            ws = f"thinair-ws-t5cache{stack}{i}"
            mgr.create_volume(ws, "t5cache")
            try:
                await _install(
                    mgr, "t5cache", ws, base.model_copy(update={"install": cmd}), print, cache
                )
            finally:
                mgr.remove_volume(ws)
        assert any(n == cache for n, _ in mgr.volumes_with_label(CACHE_LABEL))
    finally:
        mgr.remove_volume(cache)
