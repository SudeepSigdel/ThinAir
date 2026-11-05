"""Telegram notifications: message content, the sender (fake HTTP), settings, and the hook in
the job service (fake executors, no Docker, no network)."""

import asyncio

import pytest
import requests

from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.models import Job, JobKind, JobResult, JobStatus, ProfileReport, Repo
from thinair.db.session import get_engine, session_scope
from thinair.jobs.service import ExecResult, JobService
from thinair.notify import (
    NotifyConfigError,
    TelegramNotifier,
    job_message,
    notifier_from_settings,
)

BOT = "123456789:" + "AAbbCC_dd-" * 3
URL = "https://github.com/octo/demo"


@pytest.fixture(autouse=True)
def _db():
    upgrade(get_engine())


def make_job(kind=JobKind.AGENT, status=JobStatus.SUCCEEDED, **kw) -> str:
    with session_scope() as s:
        repo = Repo(url=URL + kw.pop("suffix", ""), name="demo")
        s.add(repo)
        s.commit()
        job = Job(repo_id=repo.id, kind=kind, status=status, **kw)
        s.add(job)
        s.commit()
        return job.id


# ---- message content ---------------------------------------------------------------------------
def test_agent_success_message() -> None:
    jid = make_job(task="Fix the flaky test\nmore detail that stays private")
    with session_scope() as s:
        s.add(
            JobResult(
                job_id=jid,
                diff_stat={"files": 2, "insertions": 5, "deletions": 1},
                summary="SECRET summary",
                diff_text="+secret diff",
            )
        )
        s.commit()
    msg = job_message(jid, "https://thinair.example.com/")
    assert msg.splitlines() == [
        "✅ demo: Agent task done",
        "Fix the flaky test",
        "2 file(s) changed, +5 −1",
        f"https://thinair.example.com/#/job/{jid}",
    ]
    assert "SECRET" not in msg and "secret diff" not in msg and "more detail" not in msg


def test_pr_success_links_the_pull_request() -> None:
    jid = make_job(kind=JobKind.PR, task="Fix it")
    with session_scope() as s:
        s.add(JobResult(job_id=jid, pr_url="https://github.com/octo/demo/pull/9"))
        s.commit()
    msg = job_message(jid, None)
    assert "Pull request done" in msg and "https://github.com/octo/demo/pull/9" in msg
    assert "#/job/" not in msg  # no public URL configured: no link


def test_profile_success_shows_the_minimum() -> None:
    jid = make_job(kind=JobKind.PROFILE)
    with session_scope() as s:
        s.add(ProfileReport(job_id=jid, min_config={"cpus": 0.25, "mem_bytes": 512 * 1024**2}))
        s.commit()
    assert "Minimum: 0.25 CPU / 512 MiB" in job_message(jid, None)


def test_failure_includes_the_error_and_cancelled_jobs_are_quiet() -> None:
    jid = make_job(
        status=JobStatus.FAILED, error_message="Could not clone the repository." + "x" * 400
    )
    msg = job_message(jid, None)
    assert msg.startswith("❌ demo: Agent task failed") and "Could not clone" in msg
    assert len(msg.splitlines()[-1]) <= 301
    assert job_message(make_job(status=JobStatus.CANCELLED, suffix="2"), None) is None
    assert job_message("nope", None) is None


# ---- sender --------------------------------------------------------------------------------------
class Resp:
    def __init__(self, status: int, data: dict) -> None:
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class Session:
    def __init__(self, resp=None, exc=None) -> None:
        self.resp, self.exc, self.calls = resp, exc, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp


def test_send_posts_plain_text_without_previews() -> None:
    s = Session(Resp(200, {"ok": True}))
    TelegramNotifier(BOT, "-100123", None, session=s).send_text("hello")
    url, kw = s.calls[0]
    assert url == f"https://api.telegram.org/bot{BOT}/sendMessage"
    assert kw["json"] == {"chat_id": "-100123", "text": "hello", "disable_web_page_preview": True}
    assert "parse_mode" not in kw["json"]  # plain text: nothing from a job is interpreted


@pytest.mark.parametrize(
    "session",
    [
        Session(
            exc=requests.ConnectionError(f"Max retries exceeded with url: /bot{BOT}/sendMessage")
        ),
        Session(Resp(401, {"ok": False, "description": f"Unauthorized {BOT}"})),
    ],
)
def test_send_errors_never_contain_the_bot_token(session) -> None:
    with pytest.raises(RuntimeError) as e:
        TelegramNotifier(BOT, "42", None, session=session).send_text("x")
    assert BOT not in str(e.value) and BOT.split(":")[1] not in str(e.value)


@pytest.mark.parametrize(
    ("token", "chat"), [("nope", "42"), (BOT, "chat; drop"), (BOT + "\nX", "42"), (BOT, "")]
)
def test_malformed_settings_are_refused(token, chat) -> None:
    with pytest.raises(NotifyConfigError) as e:
        TelegramNotifier(token, chat, None)
    assert BOT not in str(e.value)


def test_settings(monkeypatch) -> None:
    def setenv(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(f"THINAIR_{k.upper()}", v)
        get_settings.cache_clear()

    setenv(telegram_bot_token="", telegram_chat_id="")
    assert notifier_from_settings() is None
    setenv(telegram_bot_token=BOT)
    with pytest.raises(NotifyConfigError, match="both"):
        notifier_from_settings()
    setenv(telegram_chat_id="42", public_url="https://t.example.com")
    assert isinstance(notifier_from_settings(), TelegramNotifier)


async def test_notifier_logs_failures_instead_of_raising(caplog) -> None:
    jid = make_job(status=JobStatus.FAILED, error_message="boom")
    n = TelegramNotifier(BOT, "42", None, session=Session(Resp(500, {"description": "down"})))
    await n(jid)  # must not raise
    assert "notification not sent" in caplog.text and BOT not in caplog.text


# ---- hook in the job service -------------------------------------------------------------------
async def test_service_announces_each_finished_job_without_waiting_for_it() -> None:
    seen: list[str] = []
    release = asyncio.Event()

    async def slow_notifier(job_id: str) -> None:
        await release.wait()
        seen.append(job_id)

    async def ok(ctx, _mgr):
        return ExecResult()

    async def crash(ctx, _mgr):
        raise RuntimeError("bug")

    svc = JobService(
        {JobKind.PROFILE: ok, JobKind.AGENT: crash},
        sweep_on_start=False,
        notifier=slow_notifier,
    )
    await svc.start()
    try:
        a = svc.submit(URL)
        b = svc.submit(URL, JobKind.AGENT, task="t", agent_id="x")

        async def done() -> None:
            while True:
                with session_scope() as s:
                    if all(s.get(Job, j.id).finished_at for j in (a, b)):
                        return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(done(), 5)  # jobs finish while notifications are still pending
        assert seen == []
        release.set()
        await asyncio.wait_for(_until(lambda: len(seen) == 2), 5)
        assert sorted(seen) == sorted([a.id, b.id])
    finally:
        await svc.stop()


async def test_a_broken_notifier_never_breaks_jobs() -> None:
    async def broken(job_id: str) -> None:
        raise ValueError("notifier bug")

    async def ok(ctx, _mgr):
        return ExecResult()

    svc = JobService({JobKind.PROFILE: ok}, sweep_on_start=False, notifier=broken)
    await svc.start()
    try:
        jobs = [svc.submit(URL) for _ in range(2)]
        await asyncio.wait_for(
            _until(lambda: all(_status(j.id) == JobStatus.SUCCEEDED for j in jobs)), 5
        )
    finally:
        await svc.stop()


def _status(job_id: str) -> JobStatus:
    with session_scope() as s:
        return s.get(Job, job_id).status


async def _until(pred) -> None:
    while not pred():
        await asyncio.sleep(0.01)


def test_notify_test_command(monkeypatch) -> None:
    from typer.testing import CliRunner

    from thinair.cli.main import app as cli_app

    monkeypatch.setenv("THINAIR_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("THINAIR_TELEGRAM_CHAT_ID", "")
    get_settings.cache_clear()
    r = CliRunner().invoke(cli_app, ["notify-test"])
    assert r.exit_code == 1 and "Notifications are off" in r.output

    sent: list[str] = []
    monkeypatch.setattr(TelegramNotifier, "send_text", lambda self, text: sent.append(text))
    monkeypatch.setenv("THINAIR_TELEGRAM_BOT_TOKEN", BOT)
    monkeypatch.setenv("THINAIR_TELEGRAM_CHAT_ID", "42")
    get_settings.cache_clear()
    r = CliRunner().invoke(cli_app, ["notify-test"])
    assert r.exit_code == 0 and "Sent" in r.output and len(sent) == 1


def test_bad_telegram_settings_do_not_stop_the_server(monkeypatch, caplog) -> None:
    from fastapi.testclient import TestClient

    from thinair.api.app import create_app

    monkeypatch.setenv("THINAIR_TELEGRAM_BOT_TOKEN", "not-a-token")
    monkeypatch.setenv("THINAIR_TELEGRAM_CHAT_ID", "42")
    get_settings.cache_clear()
    with TestClient(create_app({})) as c:
        assert c.get("/healthz").status_code == 200
        assert c.app.state.jobs._notifier is None
    assert "notifications disabled" in caplog.text and "not-a-token" not in caplog.text
