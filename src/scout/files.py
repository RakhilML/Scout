"""Writing files safely: whole or not at all, and one process at a time."""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import IO

from scout.errors import ConfigError

_REPLACE_ATTEMPTS = 20  # Windows refuses to replace a file another process is reading
_NAME_ATTEMPTS = 100  # random names for a temporary file before giving up
_LOCK_WAIT = 10.0  # seconds a waiting lock tries before it reports the file busy
_RETRY_SECONDS = 0.05


def write_atomic(path: Path, text: str, *, mode: int | None = None) -> None:
    """Replace the file at *path* with *text*; a reader sees the old file or the new one, never
    half. A symbolic link at *path* is followed, as open() follows it: the link stays, and the
    file it points to (made if it is missing) gets the text; a loop of links is an OSError
    (ELOOP). On POSIX the file keeps its mode, or a new one gets *mode* less the umask (0o666
    by default, as open() gives); 0o600 keeps a new file's secrets from other users."""
    if path.is_symlink():
        path = _target(path)  # the temporary file must be beside the file it replaces
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = _temporary(path, 0o666 if mode is None else mode)
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


def _target(link: Path) -> Path:
    """The file a symbolic link leads to, through any chain of links; it may not exist yet."""
    loop = OSError(errno.ELOOP, "too many levels of symbolic links", str(link))
    try:
        target = link.resolve()
    except RuntimeError as exc:  # Python before 3.13 says so of a loop
        raise loop from exc
    if target.is_symlink():  # Python 3.13 on stops at a link of the loop
        raise loop
    return target


def _temporary(path: Path, mode: int) -> tuple[int, str]:
    """A new file beside *path* to write its next version in, open for writing.

    mkstemp makes it readable by its owner only. On POSIX it is made as open() makes a file
    instead: *mode* less the umask, which the kernel applies (reading the umask means setting
    it, for every thread at once). It then takes the mode of the file it will replace, if any."""
    if os.name == "nt":  # no modes to keep: access comes from the folder
        return tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    for _ in range(_NAME_ATTEMPTS):
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            continue
        # Nothing to replace yet, or a file system without modes: the mode stays as made.
        with contextlib.suppress(OSError):
            os.fchmod(handle, stat.S_IMODE(os.stat(path).st_mode))
        return handle, str(temporary)
    raise FileExistsError(f"no free temporary name beside {path}")


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
