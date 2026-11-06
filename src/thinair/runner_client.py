"""The program on a runner machine: `thinair runner start`.

It connects out to the server, long-polls for profile jobs, runs each one in its own local
Docker sandboxes (exactly like the server would), streams log lines and probe results back, and
reports the finished profile. One job at a time. It needs no open port.

Secrets: the runner secret comes from `THINAIR_RUNNER_TOKEN` (or a file), never the command
line (visible in `ps`). A GitHub token for private repositories is the runner's own
`THINAIR_GITHUB_TOKEN`, used only when the server says the job's owner may use one.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from thinair import __version__
from thinair.profiler.models import ProfileResult, RunRecord
from thinair.project.prepare import GitSource, PrepareError
from thinair.runners import CLAIM_WAIT_MAX_S, PROTOCOL, RUNNER_TOKEN_PREFIX
from thinair.sandbox.spec import SandboxError

log = logging.getLogger("thinair.runner")

FLUSH_S = 1.0
HEARTBEAT_S = 10.0
MAX_BUFFER = 5000
MAX_LINE = 8000


class RunnerSetupError(ValueError):
    pass


@dataclass
class Profiled:
    result: ProfileResult
    commit_sha: str | None
    stack: str | None


# (source, job_id, log, on_run, github_auth) -> Profiled
ProfileFn = Callable[
    [GitSource, str, Callable[[str], None], Callable[[RunRecord], None], bool],
    Awaitable[Profiled],
]


def check_server_url(url: str, allow_http: bool = False) -> str:
    url = url.strip().rstrip("/")
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.hostname:
        raise RunnerSetupError(f"Not a server address: {url!r} (e.g. https://thinair.example.com)")
    local = u.hostname in ("localhost", "127.0.0.1", "::1")
    if u.scheme == "http" and not (local or allow_http):
        raise RunnerSetupError(
            "The server address must use https:// (the runner secret is sent with every "
            "request). Use --allow-http only on a private network you trust."
        )
    return url


def check_token(token: str | None) -> str:
    token = (token or "").strip()
    if not token.startswith(RUNNER_TOKEN_PREFIX):
        raise RunnerSetupError(
            "Set THINAIR_RUNNER_TOKEN to the runner secret from `thinair runner add <name>` "
            f"(it starts with {RUNNER_TOKEN_PREFIX})."
        )
    return token


async def docker_profile(
    source: GitSource,
    job_id: str,
    log_fn: Callable[[str], None],
    on_run: Callable[[RunRecord], None],
    github_auth: bool,
) -> Profiled:
    from thinair.profiler.runner import profile_source
    from thinair.sandbox.manager import SandboxManager

    result, prepared = await profile_source(
        SandboxManager(), source, job_id, log=log_fn, on_run=on_run, github_auth=github_auth
    )
    return Profiled(result, prepared.commit_sha, prepared.resolved.stack)


class RunnerClient:
    def __init__(
        self,
        server: str,
        token: str,
        *,
        profile: ProfileFn = docker_profile,
        http: requests.Session | None = None,
        info: dict[str, Any] | None = None,
        wait_s: float = CLAIM_WAIT_MAX_S,
    ) -> None:
        self.server = server.rstrip("/")
        self._profile = profile
        self._http = http or requests.Session()
        self._http.headers["Authorization"] = f"Bearer {token}"
        self._http.headers["User-Agent"] = f"thinair-runner/{__version__}"
        self._info = {"protocol": PROTOCOL, "version": __version__, **(info or {})}
        self._wait = wait_s

    # ---- HTTP ------------------------------------------------------------------------------
    def _post(self, path: str, body: dict[str, Any], timeout: float = 30) -> requests.Response:
        r = self._http.post(f"{self.server}{path}", json=body, timeout=timeout)
        if r.status_code == 401:
            raise RunnerSetupError("The server refused the runner secret (wrong or disabled).")
        if r.status_code == 409:
            raise RunnerSetupError(_message(r))
        return r

    def claim(self) -> dict[str, Any] | None:
        r = self._post(f"/runner/v1/claim?wait={self._wait:g}", self._info, self._wait + 15)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json().get("job")

    # ---- main loop ---------------------------------------------------------------------------
    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        backoff = 1.0
        while stop is None or not stop.is_set():
            try:
                job = await asyncio.to_thread(self.claim)
                backoff = 1.0
            except RunnerSetupError:
                raise
            except (requests.RequestException, ValueError) as e:
                why = type(e).__name__
                log.warning("server not reachable (%s); retrying in %.0f s", why, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            if job is not None:
                await self.run_job(job)

    async def run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("id", ""))
        buffer: list[dict[str, Any]] = []

        def emit_log(line: str) -> None:
            if len(buffer) < MAX_BUFFER:
                buffer.append({"type": "log", "line": line[:MAX_LINE]})

        def emit_run(rec: RunRecord) -> None:
            buffer.append({"type": "profile_run", "run": rec.model_dump(mode="json")})

        log.info("job %s: profiling %s", job_id, job.get("repo_url"))
        work = asyncio.create_task(self._work(job, emit_log, emit_run))
        cancelled = False
        last_post = 0.0
        while not work.done():
            await asyncio.wait([work], timeout=FLUSH_S)
            if buffer or time.monotonic() - last_post >= HEARTBEAT_S:
                batch, buffer[:] = buffer[:1000], buffer[1000:]
                try:
                    r = await asyncio.to_thread(
                        self._post, f"/runner/v1/jobs/{job_id}/events", {"events": batch}
                    )
                    last_post = time.monotonic()
                    if r.ok and r.json().get("cancel"):
                        cancelled = True
                        work.cancel()
                except (requests.RequestException, ValueError):
                    buffer[:0] = batch  # keep it; try again on the next tick
                except RunnerSetupError:  # disabled while working: stop, don't orphan the job
                    work.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await work
                    raise
        if cancelled:
            log.info("job %s: cancelled by the server", job_id)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work
            return
        body = work.result()
        if buffer:
            with contextlib.suppress(requests.RequestException, ValueError):
                self._post(f"/runner/v1/jobs/{job_id}/events", {"events": buffer[:1000]})
        for attempt in range(5):
            try:
                r = await asyncio.to_thread(self._post, f"/runner/v1/jobs/{job_id}/finish", body)
                if r.ok:
                    log.info("job %s: reported (%s)", job_id, body["outcome"])
                    return
                log.warning("job %s: server rejected the result: %s", job_id, _message(r))
                return
            except (requests.RequestException, ValueError):
                await asyncio.sleep(2**attempt)
        log.error("job %s: could not report the result", job_id)

    async def _work(
        self,
        job: dict[str, Any],
        emit_log: Callable[[str], None],
        emit_run: Callable[[RunRecord], None],
    ) -> dict[str, Any]:
        """Run the job; returns the `finish` body. Never raises except on cancellation."""
        try:
            if job.get("kind") != "profile":
                return _failed("unsupported_job", "This runner only runs profile jobs.")
            source = GitSource(str(job.get("repo_url", "")), job.get("ref"))
            source.validate()  # never trust the payload: GitHub HTTPS URLs and safe refs only
            p = await self._profile(
                source, str(job["id"]), emit_log, emit_run, bool(job.get("github_auth"))
            )
            return {
                "outcome": "profiled",
                "result": p.result.model_dump(mode="json"),
                "commit_sha": p.commit_sha,
                "stack": p.stack,
            }
        except PrepareError as e:
            return _failed(e.code, e.message)
        except SandboxError as e:
            return _failed("sandbox_error", str(e))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("job %s failed unexpectedly", job.get("id"))
            return _failed("runner_error", "Something went wrong on the runner; see its logs.")


def _failed(code: str, message: str) -> dict[str, Any]:
    return {"outcome": "failed", "error_code": code, "error_message": message[:2000]}


def _message(r: requests.Response) -> str:
    try:
        detail = r.json().get("detail")
        if isinstance(detail, dict):
            return str(detail.get("message"))
        return str(detail)
    except ValueError:
        return f"HTTP {r.status_code}"
