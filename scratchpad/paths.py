"""Filesystem locations for data, configuration and runtime state.

Resolution rules (ARCHITECTURE.md section 3):

* data     ``$SCRATCHPAD_DATA_DIR`` else ``$XDG_DATA_HOME/scratchpad``
           (default ``~/.local/share/scratchpad``)
* config   ``$XDG_CONFIG_HOME/scratchpad/config.toml`` (default ``~/.config/...``)
* runtime  ``$XDG_RUNTIME_DIR/scratchpad`` (fallback ``/tmp/scratchpad-<uid>``)

The module also carries the two filesystem primitives every durable write in this
code base uses (:func:`atomic_write_bytes` and :func:`fsync_dir`) so that the
fsync discipline lives in exactly one place.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

APP_NAME = "scratchpad"

#: Directory mode for everything we create (private to the user).
DIR_MODE = 0o700
#: File mode for everything we create.
FILE_MODE = 0o600


def _env_dir(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value).expanduser()


def data_dir() -> Path:
    """Directory holding the event log, checkpoints and attachments."""
    override = _env_dir("SCRATCHPAD_DATA_DIR")
    if override is not None:
        return override
    base = _env_dir("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return base / APP_NAME


def config_dir() -> Path:
    """Directory holding ``config.toml``."""
    base = _env_dir("XDG_CONFIG_HOME") or Path.home() / ".config"
    return base / APP_NAME


def config_file() -> Path:
    """Path of the (optional) configuration file."""
    return config_dir() / "config.toml"


def runtime_dir() -> Path:
    """Directory for the IPC socket and other volatile per-boot state."""
    base = _env_dir("XDG_RUNTIME_DIR")
    if base is not None:
        return base / APP_NAME
    return Path(tempfile.gettempdir()) / f"{APP_NAME}-{os.getuid()}"


def socket_path(runtime: Path | None = None) -> Path:
    """Path of the CLI/GUI IPC socket."""
    return (runtime or runtime_dir()) / "ipc.sock"


# --- layout inside the data directory ---------------------------------------


def lock_file(data: Path) -> Path:
    """flock'd by the running GUI instance."""
    return data / "lock"


def secret_key(data: Path) -> Path:
    """32 random bytes, mode 0600, created on first use."""
    return data / "secret.key"


def history_dir(data: Path) -> Path:
    return data / "history"


def events_log(data: Path) -> Path:
    return data / "history" / "events.log"


def checkpoints_dir(data: Path) -> Path:
    return data / "history" / "checkpoints"


def index_cache(data: Path) -> Path:
    """Optional, rebuildable time index (may be absent)."""
    return data / "history" / "index.cache"


def current_text(data: Path) -> Path:
    """Latest durable full text, plain UTF-8, safe to ``cat``."""
    return data / "current.txt"


def current_meta(data: Path) -> Path:
    """Sidecar describing which log position ``current.txt`` belongs to."""
    return data / "current.meta"


def attachments_dir(data: Path) -> Path:
    return data / "attachments"


def attachments_db(data: Path) -> Path:
    return data / "attachments" / "attachments.sqlite"


def blobs_dir(data: Path) -> Path:
    return data / "attachments" / "blobs"


# --- helpers ----------------------------------------------------------------


def ensure_dir(path: Path, mode: int = DIR_MODE) -> Path:
    """Create ``path`` (and parents) if needed and return it."""
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    return path


def ensure_data_layout(data: Path) -> Path:
    """Create the directory skeleton of a data directory."""
    ensure_dir(data)
    ensure_dir(history_dir(data))
    ensure_dir(checkpoints_dir(data))
    ensure_dir(attachments_dir(data))
    ensure_dir(blobs_dir(data))
    return data


def ensure_runtime_dir() -> Path:
    """Create and return the runtime directory."""
    return ensure_dir(runtime_dir())


def fsync_dir(path: Path) -> None:
    """fsync a directory so that a rename inside it becomes durable."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = FILE_MODE) -> None:
    """Write ``data`` to ``path`` durably: temp file, fsync, rename, fsync dir.

    Every failure path removes the temp file again: a full disk or an ``fsync``
    error must not leave ``current.txt.ab12cd.tmp`` litter behind in the data
    directory (it would accumulate, and the checkpoint directory lister would
    have to explain it away).
    """
    directory = path.parent
    ensure_dir(directory)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            os.fchmod(tmp_fd, mode)
            written = 0
            view = memoryview(data)
            while written < len(view):
                written += os.write(tmp_fd, view[written:])
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(directory)


def atomic_write_text(path: Path, text: str, *, mode: int = FILE_MODE) -> None:
    """UTF-8 flavour of :func:`atomic_write_bytes`."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)
