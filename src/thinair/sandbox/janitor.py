"""Remove leftovers from crashed runs. Runs at startup and then rarely (never in a hot loop)."""

import contextlib
import logging
from datetime import UTC, datetime, timedelta

import docker
from docker.errors import DockerException

from thinair.sandbox.spec import LABEL_MANAGED

log = logging.getLogger("thinair.janitor")
_FILTER = {"label": f"{LABEL_MANAGED}=true"}


def sweep(
    client: docker.DockerClient,
    *,
    volume_max_age: timedelta = timedelta(hours=6),
    keep_job_ids: frozenset[str] = frozenset(),
) -> dict[str, int]:
    """Delete Thinair-labelled containers and stale volumes.

    At process start nothing can legitimately be running, so all labelled containers go.
    `keep_job_ids` protects jobs that are still active when called later. Volumes are only
    removed after `volume_max_age` so a just-created workspace is never raced.
    """
    removed = {"containers": 0, "volumes": 0}
    for c in client.containers.list(all=True, filters=_FILTER):
        if c.labels.get("thinair.job") in keep_job_ids:
            continue
        with contextlib.suppress(DockerException):
            c.remove(force=True, v=True)
            removed["containers"] += 1
    cutoff = datetime.now(UTC) - volume_max_age
    for v in client.volumes.list(filters=_FILTER):
        if v.attrs.get("Labels", {}).get("thinair.job") in keep_job_ids:
            continue
        created = _parse_ts(v.attrs.get("CreatedAt"))
        if created and created < cutoff:
            with contextlib.suppress(DockerException):
                v.remove(force=True)
                removed["volumes"] += 1
    if any(removed.values()):
        log.warning("janitor removed %s", removed)
    return removed


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None
