"""Pull requests from agent jobs: titles, the GitHub client (fake HTTP) and the API flow (fake
executors, no Docker). The git side is covered with real containers in test_pr_docker.py."""

import time

import pytest
from fastapi.testclient import TestClient

from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.config import get_settings
from thinair.db.models import Job, JobKind, JobResult
from thinair.db.session import session_scope
from thinair.github import GitHubAPIError, GitHubClient, repo_slug
from thinair.jobs.pr import (
    clean_body,
    clean_title,
    parse_changes,
    pr_body,
    pr_branch,
    pr_title,
)
from thinair.jobs.service import ExecResult, JobContext

ADMIN = "t" * 32
GH_TOKEN = "github_pat_" + "Ab1" * 20
URL = "https://github.com/octo/demo"
SHA = "a" * 40
PATCH = b"diff --git a/x b/x\n"


# ---- pure helpers ----------------------------------------------------------------------------
def _file(path: str, body: str, head: str = "") -> str:
    return (
        f"diff --git a/{path} b/{path}\n{head}--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,2 @@\n{body}"
    )


EDIT = _file("src/app/a.py", "-old\n+new\n-- sql comment\n ctx\n")
NEW = (
    "diff --git a/src/app/b.py b/src/app/b.py\nnew file mode 100644\n"
    "--- /dev/null\n+++ b/src/app/b.py\n@@ -0,0 +1,2 @@\n+one\n+two\n"
)
GONE = "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\n--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
MOVE = "diff --git a/a/x.py b/b/y.py\nsimilarity index 100%\nrename from a/x.py\nrename to b/y.py\n"
BIN = (
    "diff --git a/i.png b/i.png\nnew file mode 100644\nBinary files /dev/null and b/i.png differ\n"
)


def test_changes_are_counted_per_file() -> None:
    files = {f.path: f for f in parse_changes((EDIT + NEW + GONE + MOVE + BIN).encode())}
    assert (files["src/app/a.py"].added, files["src/app/a.py"].removed) == (1, 2)  # "-- sql" counts
    assert files["src/app/b.py"].kind == "added" and files["src/app/b.py"].added == 2
    assert files["old.txt"].kind == "deleted" and files["old.txt"].removed == 1
    assert files["b/y.py"].kind == "renamed" and files["b/y.py"].old_path == "a/x.py"
    assert files["i.png"].binary
    assert parse_changes(None) == [] and parse_changes(b"") == []


def test_title_describes_the_change_not_the_prompt() -> None:
    assert pr_title(EDIT) == "Update a.py"
    assert pr_title(NEW) == "Add b.py"
    assert pr_title(MOVE) == "Rename x.py to y.py"
    assert pr_title(EDIT + NEW) == "Change a.py and b.py"
    assert pr_title(
        EDIT + _file("src/app/c.py", "-a\n+b\n") + _file("src/app/d.py", "-a\n+b\n")
    ) == ("Update 3 files in src/app")
    assert pr_title(EDIT + GONE + NEW).startswith("Change 3 files")
    assert pr_title(None) == pr_title(b"garbage") == "Changes from Thinair"
    many = "".join(_file(f"dir/{'x' * 80}{i}/f.py", "-a\n+b\n") for i in range(3))
    assert len(pr_title(many)) <= 72


def test_body_lists_files_folds_agent_notes_and_leaves_out_the_prompt() -> None:
    body = pr_body(EDIT + NEW, "I fixed it.", "job1")
    assert "2 files changed, +3 / -2." in body
    assert (
        "- Updated `src/app/a.py` (+1 / -2)" in body and "- Added `src/app/b.py` (+2 / -0)" in body
    )
    assert "## Notes from the agent\n\nI fixed it." in body and "`job1`" in body
    assert body.index("## Changes") < body.index("## Notes from the agent")
    assert "## Task" not in body and "co-authored" not in body.lower()
    assert "Notes from the agent" not in pr_body(EDIT, "  ", "job1")
    assert "1 file changed" in pr_body(EDIT, None, "job1")
    big = pr_body("".join(_file(f"f{i}.py", "+a\n") for i in range(30)), None, "j")
    assert "… and 5 more" in big


def test_branch_and_slug() -> None:
    assert pr_branch("abc") == "thinair/abc"
    assert repo_slug("https://github.com/o/r.git") == ("o", "r")
    with pytest.raises(ValueError):
        repo_slug("https://gitlab.com/o/r")


# ---- GitHub client with a fake HTTP session ----------------------------------------------------
class Resp:
    def __init__(self, status: int, data) -> None:
        self.status_code, self._data = status, data
        self.content = b"x" if data is not None else b""

    def json(self):
        return self._data


class Session:
    def __init__(self, *responses: Resp) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def test_create_pull_sends_the_token_only_in_the_header() -> None:
    s = Session(Resp(201, {"html_url": "https://github.com/o/r/pull/7"}))
    url = GitHubClient(GH_TOKEN, session=s).create_pull(
        "o", "r", title="t", body="b", head="thinair/j", base="main"
    )
    assert url == "https://github.com/o/r/pull/7"
    method, called, kw = s.calls[0]
    assert (method, called) == ("POST", "https://api.github.com/repos/o/r/pulls")
    assert kw["headers"]["Authorization"] == f"Bearer {GH_TOKEN}"
    assert kw["json"] == {"title": "t", "body": "b", "head": "thinair/j", "base": "main"}
    assert GH_TOKEN not in called


def test_existing_pull_is_reused_on_retry() -> None:
    exists = Resp(
        422,
        {
            "message": "Validation Failed",
            "errors": [{"message": "A pull request already exists for o:thinair/j."}],
        },
    )
    s = Session(exists, Resp(200, [{"html_url": "https://github.com/o/r/pull/3"}]))
    url = GitHubClient(GH_TOKEN, session=s).create_pull(
        "o", "r", title="t", body="b", head="thinair/j", base="main"
    )
    assert url == "https://github.com/o/r/pull/3"
    assert s.calls[1][2]["params"] == {"head": "o:thinair/j", "state": "open"}


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (401, "rejected THINAIR_GITHUB_TOKEN"),
        (403, "Pull requests: Read and write"),
        (404, "Pull requests: Read and write"),
        (422, "No commits between main and thinair/j"),
    ],
)
def test_api_errors_are_readable_and_never_contain_the_token(status, fragment) -> None:
    data = {
        "message": "Validation Failed",
        "errors": [{"message": "No commits between main and thinair/j"}],
    }
    s = Session(Resp(status, data))
    with pytest.raises(GitHubAPIError) as e:
        GitHubClient(GH_TOKEN, session=s).create_pull(
            "o", "r", title="t", body="b", head="thinair/j", base="main"
        )
    assert fragment in e.value.message and GH_TOKEN not in e.value.message


def test_network_error_is_wrapped() -> None:
    import requests

    class Down:
        def request(self, *a, **kw):
            raise requests.ConnectionError(f"boom {GH_TOKEN}")

    with pytest.raises(GitHubAPIError) as e:
        GitHubClient(GH_TOKEN, session=Down()).default_branch("o", "r")
    assert e.value.status is None and GH_TOKEN not in e.value.message


# ---- API flow with fake executors ----------------------------------------------------------------
class Fakes:
    def __init__(self) -> None:
        self.patch: bytes | None = PATCH
        self.pr_calls: list[str] = []
        self.pr_text: list[tuple[str | None, str | None]] = []
        self.pr_fail = False

    async def agent(self, ctx: JobContext, _mgr) -> ExecResult:
        with session_scope() as s:
            s.add(JobResult(job_id=ctx.job.id, summary="ok", diff_text="+x", patch=self.patch))
            row = s.get(Job, ctx.job.id)
            row.commit_sha = SHA
            s.add(row)
            s.commit()
        return ExecResult()

    async def pr(self, ctx: JobContext, _mgr) -> ExecResult:
        from thinair.db.models import JobStatus

        self.pr_calls.append(ctx.job.parent_job_id)
        self.pr_text.append((ctx.job.pr_title, ctx.job.pr_body))
        if self.pr_fail:
            return ExecResult(JobStatus.FAILED, "push_failed", "GitHub refused the push.")
        url = "https://github.com/octo/demo/pull/1"
        with session_scope() as s:
            parent = s.get(JobResult, ctx.job.parent_job_id)
            parent.pr_url = url
            s.add(parent)
            s.add(JobResult(job_id=ctx.job.id, summary=url, pr_url=url))
            s.commit()
        return ExecResult()

    async def profile(self, ctx: JobContext, _mgr) -> ExecResult:
        return ExecResult()


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def client(monkeypatch, fakes):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", GH_TOKEN)
    get_settings.cache_clear()
    api_auth._failures.clear()
    ex = {JobKind.PROFILE: fakes.profile, JobKind.AGENT: fakes.agent, JobKind.PR: fakes.pr}
    with TestClient(create_app(ex)) as c:
        c.headers.update({"Authorization": f"Bearer {ADMIN}"})
        yield c


def finished(client, job_id: str) -> dict:
    end = time.time() + 5
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["finished"]:
            return job
        time.sleep(0.02)
    raise AssertionError(job)


def agent_job(client) -> str:
    body = {"repo_url": URL, "kind": "agent", "agent_id": "claude", "task": "fix the tests"}
    job = client.post("/api/jobs", json=body).json()
    assert finished(client, job["id"])["status"] == "succeeded"
    return job["id"]


def test_create_pr_end_to_end(client, fakes) -> None:
    aid = agent_job(client)
    r = client.get(f"/api/jobs/{aid}/result").json()
    assert r["has_patch"] and r["pr_url"] is None and r["pr_job"] is None

    resp = client.post(f"/api/jobs/{aid}/pr")
    assert resp.status_code == 201, resp.text
    prj = resp.json()
    assert prj["kind"] == "pr" and prj["parent_job_id"] == aid and prj["commit_sha"] == SHA
    assert finished(client, prj["id"])["status"] == "succeeded"
    assert fakes.pr_calls == [aid]

    r = client.get(f"/api/jobs/{aid}/result").json()
    assert r["pr_url"] == "https://github.com/octo/demo/pull/1"
    assert r["pr_job"]["id"] == prj["id"] and r["pr_job"]["status"] == "succeeded"
    assert client.get(f"/api/jobs/{prj['id']}/result").json()["pr_url"] == r["pr_url"]

    again = client.post(f"/api/jobs/{aid}/pr")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "pr_exists"


def test_failed_pr_can_be_retried(client, fakes) -> None:
    fakes.pr_fail = True
    aid = agent_job(client)
    prj = client.post(f"/api/jobs/{aid}/pr").json()
    job = finished(client, prj["id"])
    assert job["error_code"] == "push_failed"
    r = client.get(f"/api/jobs/{aid}/result").json()
    assert r["pr_job"]["status"] == "failed" and "refused" in r["pr_job"]["error_message"]
    fakes.pr_fail = False
    retry = client.post(f"/api/jobs/{prj['id']}/retry").json()
    assert retry["kind"] == "pr" and retry["parent_job_id"] == aid
    assert finished(client, retry["id"])["status"] == "succeeded"


def test_patch_download(client) -> None:
    aid = agent_job(client)
    r = client.get(f"/api/jobs/{aid}/patch")
    assert r.status_code == 200 and r.content == PATCH
    assert r.headers["content-type"].startswith("text/x-patch")
    assert "attachment" in r.headers["content-disposition"]


def test_no_changes_means_no_pr(client, fakes) -> None:
    fakes.patch = None
    aid = agent_job(client)
    r = client.post(f"/api/jobs/{aid}/pr")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "no_changes"
    assert client.get(f"/api/jobs/{aid}/patch").status_code == 404


def test_profile_jobs_cannot_become_prs(client) -> None:
    job = client.post("/api/jobs", json={"repo_url": URL}).json()
    finished(client, job["id"])
    r = client.post(f"/api/jobs/{job['id']}/pr")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "not_agent_job"
    assert client.post("/api/jobs/nope/pr").status_code == 404


def test_pr_needs_a_token(client, monkeypatch) -> None:
    aid = agent_job(client)
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "")
    get_settings.cache_clear()
    r = client.post(f"/api/jobs/{aid}/pr")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "pr_needs_token"
    assert "THINAIR_GITHUB_TOKEN" in r.json()["detail"]["message"]


async def test_real_pr_executor_fails_cleanly_without_a_token(monkeypatch, engine) -> None:
    """The real executor, no Docker: it must stop at the token check with a readable error."""
    from thinair.jobs.events import EventStore
    from thinair.jobs.pr import pr_executor

    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "")
    get_settings.cache_clear()
    job = Job(repo_id="r", kind=JobKind.PR, parent_job_id="p")
    ctx = JobContext(job=job, repo=None, events=EventStore())  # type: ignore[arg-type]
    res = await pr_executor(ctx, object())  # type: ignore[arg-type]
    assert res.error_code == "pr_needs_token"


def test_draft_is_generated_from_the_change(client) -> None:
    aid = agent_job(client)
    d = client.get(f"/api/jobs/{aid}/pr/draft").json()
    assert d["title"] == "Update x" and "## Changes" in d["body"]
    assert "fix the tests" not in d["body"]
    assert client.get("/api/jobs/nope/pr/draft").status_code == 404


def test_draft_needs_a_change(client, fakes) -> None:
    fakes.patch = None
    r = client.get(f"/api/jobs/{agent_job(client)}/pr/draft")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "no_patch"


def test_typed_title_and_description_reach_the_pr_job(client, fakes) -> None:
    fakes.pr_fail = True
    aid = agent_job(client)
    typed = {"title": "  Fix   the\nflaky test ", "body": "Why: it raced.\r\n\r\n"}
    prj = client.post(f"/api/jobs/{aid}/pr", json=typed).json()
    finished(client, prj["id"])
    assert fakes.pr_text == [("Fix the flaky test", "Why: it raced.")]
    fakes.pr_fail = False  # a retry keeps what was typed
    retry = client.post(f"/api/jobs/{prj['id']}/retry").json()
    finished(client, retry["id"])
    assert fakes.pr_text[-1] == ("Fix the flaky test", "Why: it raced.")


def test_blank_title_means_generated_and_blank_body_means_empty(client, fakes) -> None:
    aid = agent_job(client)
    prj = client.post(f"/api/jobs/{aid}/pr", json={"title": "  ", "body": ""}).json()
    finished(client, prj["id"])
    assert fakes.pr_text == [(None, "")]


def test_no_body_means_everything_generated(client, fakes) -> None:
    aid = agent_job(client)
    finished(client, client.post(f"/api/jobs/{aid}/pr").json()["id"])
    assert fakes.pr_text == [(None, None)]


def test_clean_helpers_cap_and_normalise() -> None:
    assert clean_title("x" * 999) == "x" * 256 and clean_title(" \n ") is None
    assert clean_body(None) is None and clean_body("") == ""
    assert clean_body("a\r\nb  \n") == "a\nb" and len(clean_body("y" * 99_999)) == 20_000
