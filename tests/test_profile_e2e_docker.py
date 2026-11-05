"""End-to-end profiler tests on the sample projects (need Docker; pip needs PyPI or a mirror).

Each fixture's tests hold 100-150 MB, so the expected minimum is the 256 MiB tier at every CPU
level, with the 128 MiB tier being OOM-killed.
"""

import json
from pathlib import Path

import docker
import pytest
from typer.testing import CliRunner

from thinair.cli.main import app as cli_app
from thinair.profiler.models import RunOutcome
from thinair.profiler.report import to_json, to_markdown
from thinair.profiler.runner import profile_source
from thinair.project.prepare import DirSource
from thinair.sandbox.manager import SandboxManager

MiB = 1024**2
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


@pytest.fixture(autouse=True)
def no_leaks(mgr):
    yield
    label = {"label": "thinair.managed=true"}
    assert not mgr.client.containers.list(all=True, filters=label), "container leaked"
    assert not mgr.client.volumes.list(filters=label), "workspace volume leaked"


@pytest.mark.parametrize(("fixture", "stack"), [("node_app", "node"), ("py_fastapi_app", "python")])
async def test_profiler_finds_the_256m_floor(mgr, fixture, stack) -> None:
    logs: list[str] = []
    result, prepared = await profile_source(
        mgr, DirSource(FIXTURES / fixture), f"e2e{stack}", log=logs.append
    )
    detail = "\n".join(logs[-25:]) + "\n" + to_markdown(result)
    assert result.status == "ok", detail
    assert prepared.resolved.stack == stack
    assert result.min_config is not None, detail
    assert result.min_config.mem_bytes == 256 * MiB, detail
    assert all(lv.min_mem_bytes == 256 * MiB for lv in result.levels if lv.status == "found"), (
        detail
    )

    # The 128 MiB tier really was an out-of-memory kill, and passing runs recorded metrics.
    assert any(r.mem_bytes == 128 * MiB and r.outcome == RunOutcome.OOM_KILLED for r in result.runs)
    passed = [r for r in result.runs if r.passed]
    assert passed and all(r.wall_s and r.wall_s > 0 for r in passed)
    # Metrics come from the container's own cgroup counters, so they are exact, not sampled:
    # the tests hold >= 100 MB and the run fit inside 256 MiB.
    peak = result.min_config_peak_mem_bytes
    assert peak and 100 * MiB <= peak <= 256 * MiB, detail
    assert all(r.cpu_time_s and r.cpu_time_s > 0 for r in passed), detail
    assert result.recommended and result.recommended.mem_bytes >= 256 * MiB
    assert len(result.runs) <= 16  # brute force would be 20 + stability re-runs

    md = to_markdown(result, project=fixture, stack=stack)
    assert "**Minimum passing:**" in md and "💥 out of memory" in md
    assert json.loads(to_json(result))["min_config"]["mem_bytes"] == 256 * MiB


async def test_failing_tests_give_advice_and_stop_after_one_run(mgr, tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "echo boom && exit 1"}}')
    result, _ = await profile_source(mgr, DirSource(tmp_path), "e2efail", log=lambda _: None)
    assert result.status == "baseline_failed" and len(result.runs) == 1
    assert "Fix the failing tests" in result.notes[0]
    assert "boom" in to_markdown(result)  # the failing output is shown to the user


def test_cli_profile_writes_reports(tmp_path) -> None:
    out_md, out_json = tmp_path / "r.md", tmp_path / "r.json"
    r = CliRunner().invoke(
        cli_app,
        ["profile", str(FIXTURES / "node_app"), "--md", str(out_md), "--json", str(out_json)],
    )
    assert r.exit_code == 0, r.output
    assert "Minimum passing" in out_md.read_text(encoding="utf-8")
    assert json.loads(out_json.read_text(encoding="utf-8"))["status"] == "ok"
