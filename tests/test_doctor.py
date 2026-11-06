"""`thinair doctor` without Docker: a fake Docker client, real settings and database."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from thinair import doctor
from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.session import get_engine
from thinair.doctor import render, run_checks

GiB = 1024**3


class FakeDocker:
    def __init__(
        self,
        *,
        reachable=True,
        images=True,
        cgroup="2",
        mem=8 * GiB,
        networks=True,
        volumes=("thinair-auth-claude",),
    ):
        self.reachable, self.cgroup, self.mem = reachable, cgroup, mem
        self.has_images, self.allow_networks, self.vols = images, networks, set(volumes)
        self.client = SimpleNamespace(
            info=self._info,
            networks=SimpleNamespace(list=self._networks),
            volumes=SimpleNamespace(get=self._volume),
        )

    def _info(self):
        if not self.reachable:
            raise ConnectionError("no docker")
        return {
            "ServerVersion": "29.0",
            "NCPU": 2,
            "MemTotal": self.mem,
            "CgroupVersion": self.cgroup,
        }

    def _networks(self, **_):
        if not self.allow_networks:
            raise PermissionError("403 from proxy")
        return []

    def _volume(self, name):
        if name not in self.vols:
            raise LookupError(name)
        return name

    def image_exists(self, _tag):
        return self.has_images


def by_name(checks):
    return {c.name: c for c in checks}


def test_a_healthy_install_has_no_failures(monkeypatch) -> None:
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", "a" * 40)
    monkeypatch.setenv("THINAIR_PUBLIC_URL", "https://thinair.example.com")
    get_settings.cache_clear()
    upgrade(get_engine())
    checks = run_checks(FakeDocker(), online=False)
    c = by_name(checks)
    assert all(x.status == "ok" for x in checks), render(checks)
    assert "up to date" in c["Database"].detail
    assert "claude" in c["Agent logins"].detail
    assert render(checks).endswith("Everything looks good.")


def test_problems_come_with_fixes(monkeypatch) -> None:
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", "short-token-of-20-chr")
    monkeypatch.setenv("THINAIR_RUNNER_PLACEMENT", "runners")
    monkeypatch.setenv("THINAIR_TELEGRAM_BOT_TOKEN", "not-a-bot-token")
    monkeypatch.setenv("THINAIR_TELEGRAM_CHAT_ID", "1")
    get_settings.cache_clear()
    upgrade(get_engine())
    docker = FakeDocker(images=False, cgroup="1", mem=GiB // 2, networks=False, volumes=())
    c = by_name(run_checks(docker, online=False))
    assert c["Login"].status == "warn" and "32" in c["Login"].detail
    assert c["Sandbox images"].status == "warn" and "thinair images" in c["Sandbox images"].fix
    assert c["cgroup v2"].status == "warn" and c["Memory"].status == "warn"
    assert c["Networks API"].status == "warn" and "NETWORKS" in c["Networks API"].fix
    assert c["Agent logins"].status == "warn"
    assert c["Phone access"].status == "warn"
    assert c["Notifications"].status == "fail"
    assert c["Runners"].status == "fail" and "wait forever" in c["Runners"].fix
    text = render(list(c.values()))
    assert "-> " in text and "problem(s) to fix" in text


def test_unreachable_docker_and_a_missing_database_change_nothing(tmp_path) -> None:
    checks = run_checks(FakeDocker(reachable=False), online=False)
    c = by_name(checks)
    assert c["Docker"].status == "fail" and "docker-proxy" in c["Docker"].fix
    assert "Sandbox images" not in c  # skipped when Docker is down
    assert c["Database"].status == "warn"
    assert not (get_settings().data_dir / "thinair.db").exists()  # doctor created nothing


def test_github_token_is_checked_only_against_github(monkeypatch) -> None:
    import requests

    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "github_pat_" + "A1" * 20)
    get_settings.cache_clear()
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, "Authorization" in headers))
        return SimpleNamespace(status_code=401, ok=False)

    monkeypatch.setattr(requests, "get", fake_get)
    c = by_name(run_checks(FakeDocker(), online=True))
    # The token goes only to the rate-limit check; the update check is anonymous.
    assert calls == [("https://api.github.com/rate_limit", True), (doctor.RELEASES_URL, False)]
    assert c["GitHub token"].status == "fail" and "rejects" in c["GitHub token"].detail
    assert "A1A1" not in render(list(c.values()))  # the token itself is never printed


@pytest.mark.parametrize(
    ("latest", "status", "says"),
    [
        ("v99.0.0", "warn", "v99.0.0 is available"),
        ("v0.0.1", "ok", "up to date"),
        ("nightly", "ok", "latest release: nightly"),
        (None, "ok", "couldn't check"),
    ],
)
def test_version_check(monkeypatch, latest, status, says) -> None:
    import requests

    def fake_get(url, headers, timeout):
        if latest is None:
            raise requests.ConnectionError("offline")
        return SimpleNamespace(ok=True, json=lambda: {"tag_name": latest})

    monkeypatch.setattr(requests, "get", fake_get)
    c = doctor._version(online=True)
    assert c.status == status and says in c.detail, c
    if status == "warn":
        assert "installer again" in c.fix
    assert "not checked" in doctor._version(online=False).detail


@pytest.mark.parametrize("failing", [True, False])
def test_cli_exit_code(monkeypatch, failing) -> None:
    from thinair import doctor
    from thinair.cli.main import app

    status = "fail" if failing else "ok"
    monkeypatch.setattr(doctor, "run_checks", lambda **_: [doctor.Check(status, "X", "y")])
    res = CliRunner().invoke(app, ["doctor", "--offline"])
    assert res.exit_code == (1 if failing else 0), res.output
    assert "X: y" in res.output
