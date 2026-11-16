"""Job notifications: Web Push (no account, `thinair.push`) and Telegram (an alternative).

Outbound only: Thinair calls Telegram's `sendMessage` when a job finishes. There is no webhook,
no polling and no inbound port, so idle cost stays zero. A failed send is logged (without the
bot token, which sits in the request URL) and never affects the job.

Privacy: messages contain the repo name, the job kind and status, the first line of an agent
task and the error message, and a link to the job if `THINAIR_PUBLIC_URL` is set. They pass
through Telegram's servers. Logs, diffs and summaries are never sent.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from thinair.config import get_settings
from thinair.db.models import Job, JobKind, JobResult, JobStatus, ProfileReport, Repo
from thinair.db.session import session_scope

log = logging.getLogger("thinair.notify")
Notifier = Callable[[str], Awaitable[None]]  # takes a finished job's id

BOT_TOKEN_RE = re.compile(r"^[0-9]{3,20}:[A-Za-z0-9_-]{20,100}$")
CHAT_ID_RE = re.compile(r"^(-?[0-9]{1,20}|@[A-Za-z0-9_]{5,64})$")
NOTIFY_STATUSES = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.TIMEOUT}  # not cancelled
ICONS = {JobStatus.SUCCEEDED: "✅", JobStatus.FAILED: "❌", JobStatus.TIMEOUT: "⏱"}
KIND_LABELS = {
    JobKind.PROFILE: "Resource profile",
    JobKind.AGENT: "Agent task",
    JobKind.PR: "Pull request",
}
STATUS_WORDS = {
    JobStatus.SUCCEEDED: "done",
    JobStatus.FAILED: "failed",
    JobStatus.TIMEOUT: "timed out",
}


class NotifyConfigError(Exception):
    """Telegram settings are present but malformed. The message never contains the token."""


def _mem(b: int) -> str:
    return (
        f"{b / 1024**3:g} GiB"
        if b >= 1024**3 and b % (1024**3 // 4) == 0
        else f"{b // 1024**2} MiB"
    )


@dataclass
class Summary:
    """What a finished job says about itself, for any notifier. `lines` are the detail lines
    (task's first line, result or error); the job link is added by each notifier."""

    job_id: str
    user_id: str | None
    icon: str
    title: str  # "repo: Agent task done"
    lines: list[str]


def job_summary(job_id: str) -> Summary | None:
    """The announcement for a finished job, or None if it shouldn't be announced."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        repo = s.get(Repo, job.repo_id) if job else None
        result = s.get(JobResult, job_id) if job else None
        report = s.get(ProfileReport, job_id) if job and job.kind == JobKind.PROFILE else None
    if job is None or repo is None or job.status not in NOTIFY_STATUSES:
        return None
    kind = KIND_LABELS.get(job.kind, job.kind)
    lines: list[str] = []
    if job.task and job.kind in (JobKind.AGENT, JobKind.PR):
        first = next((ln.strip() for ln in job.task.splitlines() if ln.strip()), "")
        lines.append(first[:120] + ("…" if len(first) > 120 else ""))
    if job.status == JobStatus.SUCCEEDED:
        if result and result.pr_url:
            lines.append(result.pr_url)
        elif result and job.kind == JobKind.AGENT:
            st = result.diff_stat or {}
            files, ins, dels = st.get("files", 0), st.get("insertions", 0), st.get("deletions", 0)
            lines.append(f"{files} file(s) changed, +{ins} −{dels}")
        if report and report.min_config:
            mc = report.min_config
            lines.append(
                f"Minimum: {mc.get('cpus', '?'):g} CPU / {_mem(int(mc.get('mem_bytes', 0)))}"
            )
    elif job.error_message:
        msg = job.error_message.strip()
        lines.append(msg[:300] + ("…" if len(msg) > 300 else ""))
    title = f"{repo.name}: {kind} {STATUS_WORDS[job.status]}"
    return Summary(job.id, job.user_id, ICONS[job.status], title, lines)


def job_message(job_id: str, public_url: str | None) -> str | None:
    """Plain-text message for a finished job, or None if it shouldn't be announced."""
    s = job_summary(job_id)
    if s is None:
        return None
    lines = [f"{s.icon} {s.title}", *s.lines]
    if public_url:
        lines.append(f"{public_url.rstrip('/')}/#/job/{s.job_id}")
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, public_url: str | None, session=None) -> None:
        if not BOT_TOKEN_RE.match(bot_token):
            raise NotifyConfigError(
                "THINAIR_TELEGRAM_BOT_TOKEN does not look like a bot token (123456:ABC...)."
            )
        if not CHAT_ID_RE.match(chat_id):
            raise NotifyConfigError(
                "THINAIR_TELEGRAM_CHAT_ID must be a numeric chat id or @channelname."
            )
        self._token, self._chat, self._public = bot_token, chat_id, public_url
        self._session = session

    def send_text(self, text: str) -> None:
        """Blocking send. Raises RuntimeError with a token-free message on failure."""
        import requests

        s = self._session or requests
        try:
            resp = s.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat, "text": text[:4000], "disable_web_page_preview": True},
                timeout=15,
            )
        except requests.RequestException as e:
            # The exception text contains the URL, and the URL contains the bot token.
            raise RuntimeError(f"could not reach Telegram ({type(e).__name__})") from None
        if resp.status_code != 200:
            try:
                desc = str(resp.json().get("description", ""))[:200]
            except ValueError:
                desc = ""
            raise RuntimeError(
                f"Telegram answered {resp.status_code}: {desc.replace(self._token, '***')}"
            )

    async def __call__(self, job_id: str) -> None:
        text = await asyncio.to_thread(job_message, job_id, self._public)
        if text is None:
            return
        try:
            await asyncio.to_thread(self.send_text, text)
        except RuntimeError as e:
            log.warning("job %s: notification not sent: %s", job_id, e)


def notifier_from_settings() -> Notifier | None:
    """The configured notifier, or None. Raises NotifyConfigError for malformed settings."""
    s = get_settings()
    token = s.telegram_bot_token.get_secret_value().strip() if s.telegram_bot_token else ""
    chat = (s.telegram_chat_id or "").strip()
    if not token and not chat:
        return None
    if not token or not chat:
        raise NotifyConfigError("Set both THINAIR_TELEGRAM_BOT_TOKEN and THINAIR_TELEGRAM_CHAT_ID.")
    return TelegramNotifier(token, chat, (s.public_url or "").strip() or None)


# ---- Web Push ---------------------------------------------------------------------------------
ATTENTION_TITLES = {"permission_request": "Approval needed", "waiting": "Your turn"}
PUSH_MAX_FAILURES = 5


def push_payload(title: str, body: str, job_id: str, tag: str, public_url: str | None) -> dict:
    """One shape for every browser: Declarative Web Push (iOS 18.4+ shows it natively) and, for
    the others, the service worker reads the same JSON and calls showNotification."""
    path = f"/#/job/{job_id}"
    base = (public_url or "").rstrip("/")
    navigate = f"{base}{path}" if base.startswith("https://") else path
    return {
        "web_push": 8030,
        "notification": {
            "title": title[:120],
            "body": body[:240],
            "navigate": navigate,
            "tag": tag,
        },
    }


class WebPushNotifier:
    """Sends to the *job owner's* subscribed phones (Telegram's chat gets every user's jobs; this
    is per person). Same content rules as Telegram. A failure is logged (no endpoint, which
    identifies a device) and never touches the job."""

    def __init__(self, public_url: str | None = None, session=None) -> None:
        self._public = public_url or (get_settings().public_url or "").strip() or None
        self._session = session

    # -- delivery (blocking, run in a thread) --
    def deliver(self, user_id: str | None, payload: dict) -> int:
        """Send to every subscription of this user. Returns how many were accepted."""
        from sqlmodel import select

        from thinair import push
        from thinair.db.models import PushSubscription, utcnow

        with session_scope() as s:
            q = select(PushSubscription)
            q = q.where(
                PushSubscription.user_id.is_(None)  # type: ignore[union-attr]
                if user_id is None
                else PushSubscription.user_id == user_id
            )
            subs = [(x.id, x.endpoint, x.p256dh, x.auth) for x in s.exec(q)]
        if not subs:
            return 0
        key = push.load_vapid()
        ok = 0
        for sid, endpoint, p256dh, auth in subs:
            gone, failed = False, False
            try:
                push.send(endpoint, p256dh, auth, payload, key=key, session=self._session)
                ok += 1
            except push.PushError as e:
                gone, failed = e.gone, True
                log.warning("a push could not be delivered: %s", e)
            with session_scope() as s:
                row = s.get(PushSubscription, sid)
                if row is None:
                    continue
                if gone:
                    s.delete(row)
                elif failed:
                    row.failures += 1
                    if row.failures >= PUSH_MAX_FAILURES:
                        s.delete(row)
                    else:
                        s.add(row)
                else:
                    row.failures, row.last_ok_at = 0, utcnow()
                    s.add(row)
                s.commit()
        return ok

    # -- job finished --
    async def __call__(self, job_id: str) -> None:
        summary = await asyncio.to_thread(job_summary, job_id)
        if summary is None:
            return
        body = "\n".join(summary.lines[:2])
        payload = push_payload(
            f"{summary.icon} {summary.title}", body, job_id, f"job-{job_id}", self._public
        )
        await self._safely(summary.user_id, payload, job_id)

    # -- a conversation needs the person (approval, their turn) --
    async def attention(self, job_id: str, kind: str) -> None:
        def build() -> tuple[str | None, dict] | None:
            with session_scope() as s:
                job = s.get(Job, job_id)
                repo = s.get(Repo, job.repo_id) if job else None
            if job is None or repo is None:
                return None
            first = next((ln.strip() for ln in (job.task or "").splitlines() if ln.strip()), "")
            title = f"{repo.name}: {ATTENTION_TITLES.get(kind, 'Needs you')}"
            return job.user_id, push_payload(title, first, job_id, f"job-{job_id}", self._public)

        built = await asyncio.to_thread(build)
        if built is not None:
            await self._safely(built[0], built[1], job_id)

    async def _safely(self, user_id: str | None, payload: dict, job_id: str) -> None:
        try:
            await asyncio.to_thread(self.deliver, user_id, payload)
        except Exception as e:  # noqa: BLE001 - a broken push must never affect a job
            log.warning("job %s: push not sent (%s)", job_id, type(e).__name__)
