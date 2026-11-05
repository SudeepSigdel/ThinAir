import io
import tarfile

import pytest

from thinair.project.config import ProjectConfigError, parse_project_config
from thinair.project.detect import resolve_project
from thinair.project.prepare import GitSource, PrepareError, dir_to_tar


def test_empty_config_uses_defaults() -> None:
    for text in (None, "", "  \n", "# only a comment\n"):
        cfg = parse_project_config(text)
        assert cfg.stack == "auto" and cfg.network.test is False
        assert cfg.profile.cpus == [2, 1, 0.5, 0.25]
        assert cfg.profile.memory[0] == 2 * 1024**3 and cfg.profile.memory[-1] == 128 * 1024**2


def test_full_config_parses_and_normalises() -> None:
    cfg = parse_project_config(
        """
version: 1
stack: node
install: npm ci
test: npm run check
env: {APP_ENV: test, N: 3}
workdir: web
timeouts: {test_s: 60}
network: {test: true}
profile: {cpus: [0.5, 2, 1], memory: [256m, 1g], headroom: 2}
"""
    )
    assert cfg.env == {"APP_ENV": "test", "N": "3"}
    assert cfg.profile.cpus == [2, 1, 0.5]  # sorted high -> low
    assert cfg.profile.memory == [1024**3, 256 * 1024**2]
    assert cfg.timeouts.test_s == 60 and cfg.timeouts.install_s == 600


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("stack: ruby", "stack"),
        ("tset: pytest", "tset"),  # typo
        ("system_packages: [git]", "system_packages"),  # not supported yet
        ("workdir: ../x", "workdir"),
        ("workdir: /abs", "workdir"),
        ("profile: {headroom: 0.5}", "headroom"),
        ("profile: {memory: [lots]}", "memory"),
        ("profile: {memory: [4m]}", "memory"),
        ("profile: {cpus: [0]}", "cpus"),
        ("env: {BAD-NAME: x}", "env"),
        ("- just\n- a list", "mapping"),
        ("a: [unclosed", "YAML"),
        ("version: 2", "version"),
    ],
)
def test_invalid_config_has_readable_error(text: str, fragment: str) -> None:
    with pytest.raises(ProjectConfigError) as e:
        parse_project_config(text)
    assert fragment in str(e.value)
    assert "Traceback" not in str(e.value)


def test_config_size_limit() -> None:
    with pytest.raises(ProjectConfigError, match="too large"):
        parse_project_config("env: {A: " + "x" * 70_000 + "}")


def test_detect_python_requirements() -> None:
    p = resolve_project({"requirements.txt", "app"}, parse_project_config(None))
    assert p.stack == "python" and p.image.startswith("thinair-sandbox-python")
    assert "pip install -r requirements.txt" in p.install and "pip install pytest" in p.install
    assert p.test == "python -m pytest -q"
    assert p.env["PATH"].startswith("/workspace/.venv/bin")
    assert p.workdir == "/workspace"


def test_detect_python_pyproject() -> None:
    p = resolve_project({"pyproject.toml"}, parse_project_config(None))
    assert "pip install -e ." in p.install


def test_detect_node_prefers_ci_with_lockfile() -> None:
    with_lock = resolve_project({"package.json", "package-lock.json"}, parse_project_config(None))
    assert with_lock.install == "npm ci"
    p = resolve_project({"package.json"}, parse_project_config(None))
    assert p.install == "npm install" and p.test == "npm test" and p.stack == "node"


def test_overrides_win_and_workdir_resolves() -> None:
    cfg = parse_project_config("install: make deps\ntest: make test\nworkdir: api\nenv: {X: '1'}")
    p = resolve_project({"requirements.txt"}, cfg)
    assert (p.install, p.test, p.workdir) == ("make deps", "make test", "/workspace/api")
    assert p.env["X"] == "1"


def test_undetectable_project_explains_what_to_do() -> None:
    with pytest.raises(ProjectConfigError, match="thinair.yaml"):
        resolve_project({"README.md"}, parse_project_config(None))
    # an explicit stack makes an otherwise-undetectable repo work
    assert resolve_project({"README.md"}, parse_project_config("stack: python")).stack == "python"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r",
        "https://gitlab.com/o/r",
        "https://github.com/o/r/../../x",
        "git@github.com:o/r.git",
        "file:///etc",
        "https://github.com/o",
        "--upload-pack=x",
        "https://github.com/o/r?x=1",
    ],
)
def test_only_github_https_urls_accepted(url: str) -> None:
    with pytest.raises(PrepareError) as e:
        GitSource(url).validate()
    assert e.value.code == "bad_repo_url"


@pytest.mark.parametrize(
    "url", ["https://github.com/o/r", "https://github.com/o/r.git", "https://github.com/a-b/c.d_e"]
)
def test_valid_github_urls(url: str) -> None:
    GitSource(url).validate()


def test_bad_refs_rejected() -> None:
    with pytest.raises(PrepareError):
        GitSource("https://github.com/o/r", "--upload-pack=x").validate()
    GitSource("https://github.com/o/r", "release/1.2").validate()


def test_dir_to_tar_skips_junk_and_owns_files(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("b")
    with tarfile.open(fileobj=io.BytesIO(dir_to_tar(tmp_path))) as tar:
        names = {m.name for m in tar.getmembers()}
        assert names == {"a.txt", "sub", "sub/b.py"}
        assert all(m.uid == 10001 and m.gid == 10001 for m in tar.getmembers())


def test_dir_to_tar_size_limit(tmp_path) -> None:
    (tmp_path / "big").write_bytes(b"x" * 2048)
    with pytest.raises(PrepareError, match="exceeds"):
        dir_to_tar(tmp_path, max_bytes=1024)


def test_mirror_env_only_when_set(monkeypatch) -> None:
    from thinair.config import get_settings
    from thinair.project.prepare import mirror_env

    monkeypatch.delenv("THINAIR_PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("THINAIR_NPM_REGISTRY", raising=False)
    get_settings.cache_clear()
    assert mirror_env() == {}
    monkeypatch.setenv("THINAIR_PIP_INDEX_URL", "https://mirror.example/simple")
    monkeypatch.setenv("THINAIR_NPM_REGISTRY", "https://npm.example")
    get_settings.cache_clear()
    assert mirror_env() == {
        "PIP_INDEX_URL": "https://mirror.example/simple",
        "npm_config_registry": "https://npm.example",
    }
    monkeypatch.setenv("THINAIR_PIP_INDEX_URL", "file:///etc")
    get_settings.cache_clear()
    with pytest.raises(PrepareError):
        mirror_env()


def test_sandbox_images_build_without_package_manager_network() -> None:
    """Regression: apt in the Dockerfiles stalled on networks with a broken IPv4 path."""
    from thinair.project.prepare import IMAGES_DIR

    for f in IMAGES_DIR.glob("*.Dockerfile"):
        text = f.read_text()
        assert "apt-get" not in text and "apk add" not in text, f.name


def test_every_sandbox_image_owns_workspace_as_the_sandbox_user() -> None:
    """Regression: a root-owned /workspace made `git clone` fail with 'Permission denied'."""
    from thinair.project.prepare import IMAGES_DIR

    files = sorted(IMAGES_DIR.glob("*.Dockerfile"))
    assert {f.stem for f in files} == {"python", "node", "git"}
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "chown 10001:10001 /workspace" in text and "USER 10001:10001" in text, f.name
