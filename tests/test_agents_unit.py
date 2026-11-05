"""Agent configs and output parsing (no Docker)."""

import json

import pytest

from thinair.agents.config import AgentConfigError, load_agents, parse_agent
from thinair.agents.stream import parse_line

MINIMAL = """
id: fake
name: Fake
install: "true"
command: [sh, -c, 'echo "$1"', fake, "{task}"]
auth: {login_command: [sh]}
"""


def test_builtin_claude_config_matches_the_verified_flags() -> None:
    agents, errors = load_agents()
    assert errors == []
    c = agents["claude"]
    argv = c.argv("fix the failing tests")
    assert argv[:3] == ["claude", "-p", "fix the failing tests"]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert c.output == "stream-json" and c.needs_node
    assert c.env["CLAUDE_CONFIG_DIR"] == c.auth.config_dir
    assert c.auth.check_command == ["claude", "auth", "status"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in c.auth.env_passthrough
    assert c.auth_volume == "thinair-auth-claude"
    assert "@2.1.311" in c.install  # pinned, never "latest"


def test_task_is_always_one_argument_and_never_a_flag() -> None:
    c = parse_agent(MINIMAL)
    nasty = 'fix it"; rm -rf / #  $(whoami) `id`'
    assert c.argv(nasty)[-1] == nasty  # stays a single argv item, no shell involved
    assert c.argv("--dangerous-option")[-1] == " --dangerous-option"


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ("id: Bad_Id", "id"),
        ('command: [run, "{task}", "{task}"]', "exactly once"),
        ("command: [run, '--prompt={task}']", "exactly once"),
        ('command: [run, "{task}", "{model}"]', "unknown placeholder"),
        ("unknown_key: 1", "unknown_key"),
        ("output: xml", "output"),
        ("limits: {memory: lots}", "memory"),
        ("env: {BAD-NAME: x}", "env"),
    ],
)
def test_invalid_configs_name_the_problem(change: str, fragment: str) -> None:
    key = change.split(":")[0]
    lines = [ln for ln in MINIMAL.strip().splitlines() if not ln.startswith(key + ":")]
    with pytest.raises(AgentConfigError) as e:
        parse_agent("\n".join([*lines, change]), "my-agent.yaml")
    assert fragment in str(e.value) and "my-agent.yaml" in str(e.value)


def test_auth_hint_and_config_dir_validation() -> None:
    c = parse_agent(MINIMAL)
    assert "thinair auth fake" in c.auth_hint
    with pytest.raises(AgentConfigError, match="absolute"):
        parse_agent(
            MINIMAL.replace(
                "auth: {login_command: [sh]}", "auth: {login_command: [sh], config_dir: rel}"
            )
        )


def test_user_agents_are_added_and_can_override_builtins(tmp_path) -> None:
    (tmp_path / "fake.yaml").write_text(MINIMAL)
    (tmp_path / "claude.yaml").write_text(
        MINIMAL.replace("id: fake", "id: claude").replace("Fake", "My Claude")
    )
    (tmp_path / "broken.yaml").write_text("id: [")
    agents, errors = load_agents(tmp_path)
    assert agents["fake"].name == "Fake"
    assert agents["claude"].name == "My Claude"
    assert len(errors) == 1 and "broken.yaml" in errors[0]


# ---- stream parsing ------------------------------------------------------------------------
def line(obj) -> str:
    return json.dumps(obj)


def test_assistant_text_and_tool_calls_become_short_events() -> None:
    evs = parse_line(
        line(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "  I'll run the tests first.  "},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pytest -q   tests/"},
                        },
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "app/main.py", "old": "x"},
                        },
                        {"type": "tool_use", "name": "Mystery", "input": {"a": 1}},
                    ]
                },
            }
        )
    )
    assert [(e.kind, e.text) for e in evs] == [
        ("text", "I'll run the tests first."),
        ("tool", "Bash: pytest -q tests/"),
        ("tool", "Edit: app/main.py"),
        ("tool", "Mystery"),
    ]


def test_result_message_carries_summary_and_cost() -> None:
    (ev,) = parse_line(
        line(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Fixed the off-by-one in add().",
                "total_cost_usd": 0.042,
                "num_turns": 6,
                "session_id": "abc",
            }
        )
    )
    assert ev.kind == "result" and ev.text.startswith("Fixed")
    assert ev.data["is_error"] is False and ev.data["total_cost_usd"] == 0.042


def test_auth_failures_are_recognised() -> None:
    (ev,) = parse_line(
        line({"type": "system", "subtype": "api_retry", "error": "authentication_failed"})
    )
    assert ev.kind == "auth_error"
    (other,) = parse_line(line({"type": "system", "subtype": "api_retry", "error": "rate_limit"}))
    assert other.kind == "log"


@pytest.mark.parametrize(
    "raw",
    ["plain text from the CLI", "[1, 2]", '{"type": "something_new", "x": 1}', "{not json"],
)
def test_unknown_output_is_logged_never_fatal(raw: str) -> None:
    evs = parse_line(raw)
    assert len(evs) == 1 and evs[0].kind == "log"


def test_user_tool_results_are_skipped_and_text_format_is_passthrough() -> None:
    assert parse_line(line({"type": "user", "message": {"content": []}})) == []
    assert parse_line('{"type": "result"}', fmt="text")[0].kind == "log"


def test_long_values_are_shortened() -> None:
    (ev,) = parse_line(
        line(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 1000}}
                    ]
                },
            }
        )
    )
    assert len(ev.text) < 200 and ev.text.endswith("…")
