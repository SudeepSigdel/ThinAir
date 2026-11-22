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


# ---- finding a project below the top folder ------------------------------------------------------
def test_a_project_in_a_subfolder_is_found() -> None:
    from thinair.project.detect import choose_workdir

    # One project, in a subfolder: use it, with no warning.
    assert choose_workdir(["backend/requirements.txt", "backend/app/x.txt"]) == ("backend", None)
    assert choose_workdir(["web/package.json"]) == ("web", None)
    assert choose_workdir(["services/api/pyproject.toml"]) == ("services/api", None)
    assert choose_workdir([]) == (None, None)
    assert choose_workdir(["notes/README.md", "x/y/z/package.json"]) == (None, None)  # too deep


def test_several_projects_pick_one_and_say_so() -> None:
    from thinair.project.detect import choose_workdir

    paths = ["frontend/package.json", "backend/requirements.txt", "docs/package.json"]
    chosen, note = choose_workdir(paths)
    # Deterministic: python before node (a profile needs tests; Python projects ship them more
    # often), then the shallowest, then by name.
    assert chosen == "backend"
    assert note and "backend" in note and "frontend" in note and "workdir" in note
    # Same stack: shallowest first, then alphabetical.
    chosen, _ = choose_workdir(["b/package.json", "a/c/package.json", "a/package.json"])
    assert chosen == "a"


def test_dependency_and_hidden_folders_never_count() -> None:
    from thinair.project.detect import choose_workdir

    junk = [
        "node_modules/left-pad/package.json",
        "web/node_modules/x/package.json",
        ".venv/lib/pyproject.toml",
        ".github/package.json",
        "vendor/pkg/setup.py",
        "site-packages/x/setup.py",
    ]
    assert choose_workdir(junk) == (None, None)
    assert choose_workdir([*junk, "app/setup.py"]) == ("app", None)


def test_the_error_says_what_was_looked_at() -> None:
    from thinair.project.config import ProjectConfig, ProjectConfigError

    with pytest.raises(ProjectConfigError) as e:
        resolve_project({"README.md", "pom.xml", "src"}, ProjectConfig())
    text = str(e.value)
    assert "pom.xml" in text and "Python and Node.js" in text and "workdir" in text


# ---- Node projects: npm, pnpm and yarn ----------------------------------------------------------
def _node(*files: str):
    from thinair.project.config import ProjectConfig
    from thinair.project.detect import resolve_project

    return resolve_project({"package.json", *files}, ProjectConfig())


def test_node_projects_use_the_package_manager_their_lockfile_names() -> None:
    assert _node().install == "npm install" and _node().test == "npm test"
    assert _node("package-lock.json").install == "npm ci"

    pnpm = _node("pnpm-lock.yaml")
    # corepack runs pnpm without `corepack enable` (the root filesystem is read-only); the frozen
    # install fails on a stale lockfile instead of silently rewriting it, like `npm ci`.
    assert pnpm.install == "corepack pnpm install --frozen-lockfile"
    assert pnpm.test == "corepack pnpm test"
    # The test step has no network and a throwaway /tmp: pnpm's download must sit on the shared
    # workspace volume, or the tests could not start it.
    assert pnpm.env["COREPACK_HOME"] == "/workspace/.corepack"
    assert pnpm.env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] == "0"

    yarn = _node("yarn.lock")
    assert yarn.install == "yarn install --frozen-lockfile" and yarn.test == "yarn test"
    assert "COREPACK_HOME" not in yarn.env and "COREPACK_HOME" not in _node().env


def test_several_lockfiles_pick_pnpm_then_yarn_then_npm() -> None:
    assert _node("pnpm-lock.yaml", "yarn.lock", "package-lock.json").install.startswith(
        "corepack pnpm"
    )
    assert _node("yarn.lock", "package-lock.json").install.startswith("yarn ")


def test_thinair_yaml_still_overrides_the_detected_commands() -> None:
    from thinair.project.config import ProjectConfig
    from thinair.project.detect import resolve_project

    cfg = ProjectConfig(install="make deps", test="make check")
    r = resolve_project({"package.json", "pnpm-lock.yaml"}, cfg)
    assert r.install == "make deps" and r.test == "make check"


def test_python_projects_are_unaffected() -> None:
    from thinair.project.config import ProjectConfig
    from thinair.project.detect import resolve_project

    r = resolve_project({"requirements.txt", "pnpm-lock.yaml"}, ProjectConfig())
    assert r.stack == "python" and "COREPACK_HOME" not in r.env
