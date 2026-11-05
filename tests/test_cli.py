import io
import sys

from thinair.cli.main import _utf8_output


def test_output_survives_legacy_windows_console_encoding(monkeypatch) -> None:
    """Regression: printing the report's emoji crashed with cp1252 stdout."""
    raw = io.BytesIO()
    legacy = io.TextIOWrapper(raw, encoding="cp1252", line_buffering=True)
    monkeypatch.setattr(sys, "stdout", legacy)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    _utf8_output()
    print("✅ passed 💥 out of memory")
    legacy.flush()
    assert raw.getvalue().decode("utf-8").strip() == "✅ passed 💥 out of memory"


def test_init_writes_env_with_a_fresh_token_and_never_clobbers(tmp_path) -> None:
    from typer.testing import CliRunner

    from thinair.cli.main import app as cli_app

    env = tmp_path / ".env"
    r = CliRunner().invoke(cli_app, ["init", "--path", str(env)])
    assert r.exit_code == 0, r.output
    token = next(
        ln.split("=", 1)[1]
        for ln in env.read_text(encoding="utf-8").splitlines()
        if ln.startswith("THINAIR_ADMIN_TOKEN=")
    )
    assert token.startswith("thinair_") and len(token) >= 40 and token in r.output
    assert len(token) >= 20  # satisfies the server's minimum
    again = CliRunner().invoke(cli_app, ["init", "--path", str(env)])
    assert again.exit_code == 1 and "already exists" in again.output
    assert token in env.read_text(encoding="utf-8")  # untouched
    forced = CliRunner().invoke(cli_app, ["init", "--path", str(env), "--force"])
    assert forced.exit_code == 0 and token not in env.read_text(encoding="utf-8")


def test_agents_command_lists_claude() -> None:
    from typer.testing import CliRunner

    from thinair.cli.main import app as cli_app

    r = CliRunner().invoke(cli_app, ["agents"])
    assert r.exit_code == 0 and "claude" in r.output and "Claude Code" in r.output


def test_auth_unknown_agent_is_a_clear_error() -> None:
    from typer.testing import CliRunner

    from thinair.cli.main import app as cli_app

    r = CliRunner().invoke(cli_app, ["auth", "nope"])
    assert r.exit_code == 1 and "Unknown agent" in r.output and "claude" in r.output


def test_auth_prints_a_hardened_login_command_without_any_tokens(monkeypatch) -> None:
    from typer.testing import CliRunner

    import thinair.agents.cli_adapter as adapter_mod
    import thinair.cli.main as cli

    async def fake_image(_mgr, _cfg, _stack, _log):
        return "thinair-agent-claude-node:abc"

    class FakeMgr:
        def ensure_persistent_volume(self, name, labels):
            assert name == "thinair-auth-claude" and "thinair.managed" not in labels

    monkeypatch.setattr(adapter_mod, "ensure_agent_image", fake_image)
    monkeypatch.setattr(cli, "SandboxManager", FakeMgr)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "should-never-appear")
    r = CliRunner().invoke(cli.app, ["auth", "claude", "--print-command"])
    assert r.exit_code == 0, r.output
    assert "--user 10001:10001" in r.output and "--cap-drop ALL" in r.output
    assert "-v thinair-auth-claude:/thinair-auth" in r.output
    assert "thinair-agent-claude-node:abc claude" in r.output
    assert "should-never-appear" not in r.output and "/login" in r.output
