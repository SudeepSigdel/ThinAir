"""Small secrets store: files under `$THINAIR_DATA_DIR/secrets/`, never the database.

The directory is 0700 and each file 0600, written to a temp file and renamed, so a reader never
sees a half-written secret and nobody else on the host can read it. Used for the GitHub App's
private key (`github-app.json`). Sandboxes never get this directory: they only mount per-job
workspace, login and cache volumes, and `SandboxSpec` refuses the server's data volume.
"""

import contextlib
import os
import re
import stat
import tempfile
from pathlib import Path

from thinair.config import get_settings

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class SecretError(Exception):
    """A secret could not be stored or read. The message never contains the secret."""


def _dir() -> Path:
    return get_settings().data_dir / "secrets"


def _path(name: str) -> Path:
    if not _NAME.match(name) or ".." in name:
        raise SecretError("invalid secret name")
    return _dir() / name


def write_secret(name: str, data: bytes) -> Path:
    path = _path(name)
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(d, 0o700)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:  # mkstemp creates it 0600
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def read_secret(name: str) -> bytes | None:
    try:
        return _path(name).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SecretError(f"could not read secret {name!r}: {e.strerror}") from None


def delete_secret(name: str) -> bool:
    try:
        _path(name).unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        raise SecretError(f"could not delete secret {name!r}: {e.strerror}") from None
    return True


def secret_mode_problem(name: str) -> str | None:
    """Why a stored secret is unsafe (readable by group/others), or None. POSIX only."""
    if os.name != "posix":
        return None
    try:
        st = _path(name).stat()
    except OSError:
        return None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"secret {name!r} is readable by other users (mode {stat.S_IMODE(st.st_mode):o})"
    return None
