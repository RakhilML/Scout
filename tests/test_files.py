import errno
import os
import stat
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from scout.errors import ConfigError
from scout.files import FileLock, write_atomic

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


@contextmanager
def umask(mask: int):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@posix
def test_a_new_file_gets_the_mode_open_gives_it_not_an_owner_only_one(tmp_path):
    path = tmp_path / "notes" / "report.html"
    with umask(0o027):
        write_atomic(path, "<p>hi</p>")
    assert mode(path) == 0o640
    with umask(0o022):
        write_atomic(tmp_path / "other.html", "")
    assert mode(tmp_path / "other.html") == 0o644


@posix
def test_a_replaced_file_keeps_its_mode(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o604)
    with umask(0o077):
        write_atomic(path, "new")
    assert (path.read_text(encoding="utf-8"), mode(path)) == ("new", 0o604)
    assert [p.name for p in tmp_path.iterdir()] == ["report.md"]


def symlink(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target)
    except OSError as exc:  # Windows lets only some users make links
        pytest.skip(f"cannot make a symbolic link here: {exc}")


def test_a_symbolic_link_is_written_through_and_kept(tmp_path):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "scout.service"
    target.write_text("old", encoding="utf-8")
    symlink(tmp_path / "linked.service", target)
    link = tmp_path / "scout.service"
    symlink(link, tmp_path / "linked.service")  # a link to a link
    write_atomic(link, "new")

    assert link.is_symlink()
    assert link.resolve() == target.resolve()
    assert (target.read_text(encoding="utf-8"), link.read_text(encoding="utf-8")) == ("new",) * 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "dotfiles",
        "linked.service",
        "scout.service",
    ]
    assert [p.name for p in dotfiles.iterdir()] == ["scout.service"]  # no temporary file left


def test_a_dangling_symbolic_link_makes_its_target(tmp_path):
    target = tmp_path / "reports" / "report.md"
    link = tmp_path / "report.md"
    symlink(link, target)
    write_atomic(link, "new")
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("links", [1, 2])
def test_a_loop_of_symbolic_links_is_an_os_error_and_changes_nothing(tmp_path, links):
    one, other = tmp_path / "gpu.xml", tmp_path / "other.xml"
    if links == 1:
        symlink(one, one)
    else:
        symlink(one, other)
        symlink(other, one)
    with pytest.raises(OSError, match="too many levels of symbolic links") as raised:
        write_atomic(one, "<feed/>")
    assert raised.value.errno == errno.ELOOP
    assert one.is_symlink()  # not replaced by a file
    names = ["gpu.xml", "other.xml"][:links]
    assert sorted(p.name for p in tmp_path.iterdir()) == names  # and no temporary file left


@posix
def test_a_new_file_can_be_made_for_its_owner_only(tmp_path):
    path = tmp_path / "watches.yaml"
    with umask(0o022):
        write_atomic(path, "watches: []\n", mode=0o600)
        assert mode(path) == 0o600
        path.chmod(0o640)  # its owner opened it to a group: that stays
        write_atomic(path, "watches: []\n", mode=0o600)
    assert mode(path) == 0o640


@posix
def test_a_linked_file_keeps_its_mode_and_a_new_target_gets_the_umask_one(tmp_path):
    target = tmp_path / "target.md"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o604)
    link = tmp_path / "link.md"
    link.symlink_to(target)
    dangling = tmp_path / "dangling.md"
    dangling.symlink_to(tmp_path / "made.md")
    with umask(0o027):
        write_atomic(link, "new")
        write_atomic(dangling, "new")
    assert (mode(target), mode(tmp_path / "made.md")) == (0o604, 0o640)
    assert (link.is_symlink(), dangling.is_symlink()) == (True, True)


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
