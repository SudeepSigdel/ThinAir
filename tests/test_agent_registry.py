"""Agents from the ACP registry: config generation (offline, with fake HTTP) and the download
build stage. The real registry + real images are checked in test_registry_agents_docker.py."""

import base64
import re

import pytest
import yaml
from typer.testing import CliRunner

from thinair.agents import registry as reg
from thinair.agents.cli_adapter import agent_dockerfile
from thinair.agents.config import AgentConfig, load_agents
from thinair.cli.main import app as cli
from thinair.config import get_settings

SHA = "a" * 64
REGISTRY = {
    "version": "1",
    "agents": [
        {
            "id": "copilot-like",
            "name": "Copilot-like",
            "version": "1.0.1",
            "distribution": {"npx": {"package": "@gh/copilot@1.0.1", "args": ["--acp"]}},
        },
        {
            "id": "binary-like",
            "name": "Binary-like",
            "version": "2.0.0",
            "repository": "https://github.com/x/binary-like",
            "distribution": {
                "binary": {
                    "linux-x86_64": {
                        "archive": "https://example.com/b-x64.tar.gz",
                        "cmd": "./bin/b",
                        "args": ["acp"],
                        "sha256": SHA,
                        "env": {"B_ACP": "1"},
                    },
                    "linux-aarch64": {
                        "archive": "https://example.com/b-arm64.tar.gz",
                        "cmd": "./bin/b",
                        "args": ["acp"],
                        "sha256": SHA,
                    },
                    "darwin-aarch64": {"archive": "https://example.com/mac.zip", "cmd": "./b"},
                }
            },
        },
        {
            "id": "per-arch",
            "name": "Per-arch",
            "version": "1.0.0",
            "distribution": {
                "binary": {
                    "linux-x86_64": {"archive": "https://e.com/a.tar.gz", "cmd": "./pool-amd64"},
                    "linux-aarch64": {"archive": "https://e.com/b.tar.gz", "cmd": "./pool-arm64"},
                }
            },
        },
        {"id": "py-only", "name": "Py", "version": "1", "distribution": {"uvx": {"package": "p"}}},
        {
            "id": "mac-only",
            "name": "Mac",
            "version": "1",
            "distribution": {"binary": {"darwin-aarch64": {"archive": "https://e/m", "cmd": "m"}}},
        },
        {
            "id": "evil",
            "name": "Evil",
            "version": "1",
            "distribution": {
                "binary": {
                    "linux-x86_64": {"archive": "https://e.com/x.tgz", "cmd": "../../bin/sh"}
                }
            },
        },
    ],
}
NPM = {"@gh/copilot": {"bin": {"copilot": "npm-loader.js"}}}


def fake_get(url: str):
    if url == reg.REGISTRY_URL:
        return REGISTRY
    name = url.split("registry.npmjs.org/")[1].rsplit("/", 1)[0].replace("%2F", "/")
    return NPM[name]


def entry(aid: str) -> dict:
    return next(e for e in REGISTRY["agents"] if e["id"] == aid)


def test_npm_agent_becomes_a_pinned_acp_config() -> None:
    cfg = reg.config_for(entry("copilot-like"), fake_get)
    assert cfg["install"] == "npm install -g @gh/copilot@1.0.1"
    assert cfg["command"] == ["copilot", "--acp"]
    assert cfg["protocol"] == "acp" and cfg["needs_node"] is True
    assert cfg["env"]["HOME"] == "/thinair-auth"  # the agent's own login dir is in its volume
    assert cfg["auth"]["login_command"] == ["copilot"]
    assert AgentConfig.model_validate(cfg).auth_volume == "thinair-auth-copilot-like"


def test_binary_agent_downloads_linux_builds_only_with_checksums() -> None:
    cfg = reg.config_for(entry("binary-like"), fake_get)
    assert set(cfg["download"]) == {"linux-x86_64", "linux-aarch64"}  # no macOS
    assert cfg["download"]["linux-x86_64"] == {
        "url": "https://example.com/b-x64.tar.gz",
        "sha256": SHA,
    }
    assert cfg["command"] == ["/opt/thinair-agent/bin/b", "acp"]
    assert cfg["env"]["B_ACP"] == "1" and "install" not in cfg
    text = reg.to_yaml(cfg, entry("binary-like"))
    assert "github.com/x/binary-like" in text and "no checksum" not in text
    assert AgentConfig.model_validate(yaml.safe_load(text)).download is not None


def test_per_architecture_program_names_are_linked_to_one_command() -> None:
    cfg = reg.config_for(entry("per-arch"), fake_get)
    assert cfg["command"] == ["/opt/thinair-agent/thinair-agent-cmd"]
    assert cfg["download"]["linux-aarch64"]["cmd"] == "pool-arm64"
    assert "no checksum" in reg.to_yaml(cfg, entry("per-arch"))


@pytest.mark.parametrize(
    ("aid", "why"),
    [("py-only", "uvx"), ("mac-only", "no Linux build"), ("evil", "unexpected command path")],
)
def test_agents_thinair_cannot_run_are_refused(aid, why) -> None:
    with pytest.raises(reg.RegistryError, match=why):
        reg.config_for(entry(aid), fake_get)


def test_which_command_an_npm_package_installs() -> None:
    def get(meta):
        return lambda _url: meta

    assert reg.npm_bin("@scope/tool", "1", get({"bin": "cli.js"})) == "tool"
    assert reg.npm_bin("x", "1", get({"bin": {"a": "1.js", "x": "2.js"}})) == "x"
    assert reg.npm_bin("@k/cli", "1", get({"bin": {"kilocode": "b", "kilo": "b"}})) == "kilo"
    with pytest.raises(reg.RegistryError):
        reg.npm_bin("x", "1", get({"bin": {"a": "1.js", "b": "2.js"}}))
    assert reg.split_package("@a/b@1.2.3") == ("@a/b", "1.2.3")
    with pytest.raises(reg.RegistryError):
        reg.split_package("unpinned")


def test_download_stage_is_safe_to_embed() -> None:
    cfg = AgentConfig.model_validate(reg.config_for(entry("binary-like"), fake_get))
    df = agent_dockerfile(cfg, "thinair-sandbox-node:2", "node")
    lines = df.splitlines()
    assert lines[0] == "FROM python:3.12-slim AS thinair-fetch"
    assert "COPY --from=thinair-fetch /opt/thinair-agent /opt/thinair-agent" in lines
    code = base64.b64decode(re.search(r"b64decode\('([A-Za-z0-9+/=]+)'\)", df).group(1)).decode()
    assert SHA in code and "checksum mismatch" in code and 'filter="data"' in code
    compile(code, "fetch", "exec")  # valid Python
    with pytest.raises(ValueError, match="url"):
        AgentConfig.model_validate(
            {**cfg.model_dump(), "download": {"linux-x86_64": {"url": "http://plain.example/x"}}}
        )
    with pytest.raises(ValueError, match="install: or download:"):
        AgentConfig.model_validate({**cfg.model_dump(), "download": None, "install": None})


def test_cli_search_add_remove(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THINAIR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(reg, "_get_json", fake_get)
    monkeypatch.setattr(reg, "fetch_registry", lambda get=None: REGISTRY["agents"])
    runner = CliRunner()
    out = runner.invoke(cli, ["agent", "search"]).output
    assert "copilot-like" in out and "unsupported (Python/uvx)" in out
    res = runner.invoke(cli, ["agent", "add", "copilot-like"])
    assert res.exit_code == 0, res.output
    assert "copilot-like" in load_agents(tmp_path / "agents")[0]
    assert "added" in runner.invoke(cli, ["agent", "search", "copilot"]).output
    assert runner.invoke(cli, ["agent", "add", "copilot-like"]).exit_code == 1  # already there
    assert runner.invoke(cli, ["agent", "add", "copilot-like", "--force"]).exit_code == 0
    assert runner.invoke(cli, ["agent", "add", "nope"]).exit_code == 1
    assert runner.invoke(cli, ["agent", "add", "py-only"]).exit_code == 1
    assert runner.invoke(cli, ["agent", "remove", "copilot-like"]).exit_code == 0
    assert "copilot-like" not in load_agents(tmp_path / "agents")[0]
    assert runner.invoke(cli, ["agent", "remove", "claude"]).exit_code == 1  # built-in


def test_cli_refuses_to_shadow_a_builtin(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THINAIR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    claude = {
        "id": "claude-acp",
        "name": "Claude",
        "version": "9",
        "distribution": {"npx": {"package": "c@9"}},
    }
    monkeypatch.setattr(reg, "fetch_registry", lambda get=None: [claude])
    res = CliRunner().invoke(cli, ["agent", "add", "claude-acp"])
    assert res.exit_code == 1 and "built-in" in res.output
