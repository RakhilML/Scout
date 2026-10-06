"""Writing files safely: whole or not at all, and one process at a time."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import IO

from scout.errors import ConfigError

_REPLACE_ATTEMPTS = 20  # Windows refuses to replace a file another process is reading
_LOCK_WAIT = 10.0  # seconds a waiting lock tries before it reports the file busy
_RETRY_SECONDS = 0.05


def write_atomic(path: Path, text: str) -> None:
    """Replace *path* with *text*; a reader sees the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as file:
            file.write(text)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_RETRY_SECONDS)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class FileLock:
    """An exclusive lock on *path* across processes. Without *wait* a held lock is an error at
    once; with it, after about ten seconds."""

    def __init__(self, path: Path, *, wait: bool = True, busy: str = "is in use") -> None:
        self.path = path
        self._wait = wait
        self._busy = busy
        self._file: IO[str] | None = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a", encoding="utf-8")
        deadline = time.monotonic() + (_LOCK_WAIT if self._wait else 0.0)
        while True:
            try:
                _lock(handle)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise ConfigError(f"{self._busy} ({self.path})") from exc
                time.sleep(_RETRY_SECONDS)
        self._file = handle
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._file is not None:
            _unlock(self._file)
            self._file.close()
            self._file = None


if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
