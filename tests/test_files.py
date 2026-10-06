import threading

import pytest

from scout.errors import ConfigError
from scout.files import FileLock, write_atomic


def test_a_file_being_read_is_replaced_once_the_reader_lets_go(tmp_path):
    path = tmp_path / "watches.yaml"
    path.write_text("old", encoding="utf-8")
    reader = path.open(encoding="utf-8")
    threading.Timer(0.2, reader.close).start()
    write_atomic(path, "new")
    assert path.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["watches.yaml"]


def test_a_waiting_lock_gets_the_file_once_it_is_free(tmp_path):
    path = tmp_path / "x.lock"
    held = FileLock(path)
    held.__enter__()
    with pytest.raises(ConfigError, match="busy"), FileLock(path, wait=False, busy="busy"):
        pass
    threading.Timer(0.2, held.__exit__, args=(None, None, None)).start()
    with FileLock(path):
        pass
