import logging
import os
import threading
import time
from datetime import timedelta

import pytest

from scout.app import App
from scout.errors import ConfigError, SearchError
from scout.files import FileLock
from scout.monitor.daemon import RETRY_AFTER, Daemon
from scout.monitor.runner import WatchRun
from scout.monitor.watches import Watch, WatchBook
from scout.settings import Settings
from tests.helpers import NOW, SAMPLE_RESULT, Clock


@pytest.fixture
def app(tmp_path):
    with App(Settings(data_dir=tmp_path, reports_dir=tmp_path / "reports")) as app:
        yield app


@pytest.fixture
def book(tmp_path):
    return WatchBook(tmp_path / "watches.yaml")


def touch(book: WatchBook) -> None:
    """Make an edit visible even on file systems with coarse modification times."""
    stat = book.path.stat()
    os.utime(book.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def test_the_schedule_follows_the_watches_file(app, book):
    daemon = Daemon(app, book, clock=Clock())
    daemon.sync()
    assert daemon.scheduled == set()

    book.add(Watch(name="gpu", goal="cheapest RTX 5090", every="6h"))
    book.add(Watch(name="py", goal="Python news", cron="0 9 * * *", paused=True))
    daemon.sync()
    assert daemon.scheduled == {"gpu"}

    book.update("py", paused=False)
    book.remove("gpu")
    touch(book)
    daemon.sync()
    assert daemon.scheduled == {"py"}

    book.path.write_text("watches: [", encoding="utf-8")  # a half-saved edit
    touch(book)
    daemon.sync()
    assert daemon.scheduled == {"py"}  # the last good schedule stays


def test_interval_watches_run_when_due_and_cron_watches_by_the_calendar(app, book):
    clock = Clock()
    daemon = Daemon(app, book, clock=clock)
    fresh = Watch(name="fresh", goal="g", every="6h")
    assert daemon._due(fresh) == NOW  # never ran: now

    app.store.record("fresh", SAMPLE_RESULT, [])  # ran at NOW
    clock.advance(3600)
    assert daemon._due(fresh) == NOW + timedelta(hours=6)
    clock.advance(10 * 3600)
    assert daemon._due(fresh) == clock.now  # overdue: now
    assert daemon._due(Watch(name="cal", goal="g", cron="0 9 * * *")) is None


def test_a_failing_watch_is_logged_not_raised(app, book, monkeypatch, caplog):
    book.add(Watch(name="gpu", goal="cheapest RTX 5090", every="6h"))

    def fail(*args, **kwargs):
        raise SearchError("no search results for: rtx")

    monkeypatch.setattr("scout.monitor.daemon.run_watch", fail)
    daemon = Daemon(app, book, clock=Clock())
    with caplog.at_level(logging.ERROR, logger="scout"):
        daemon.run("gpu")
        daemon.run("removed-meanwhile")
    assert "gpu failed: no search results for: rtx" in caplog.text
    assert "removed-meanwhile failed: no watch named" in caplog.text
    retries = {job.id: job.trigger.run_date for job in daemon._scheduler.get_jobs()}
    assert retries == {"gpu:retry": NOW + RETRY_AFTER}  # a missing watch is not retried


@pytest.mark.parametrize("name", ["gpu", "prune"])
def test_serve_runs_what_is_due_then_stops_cleanly(app, book, monkeypatch, name):
    book.add(Watch(name=name, goal="cheapest RTX 5090", every="6h"))
    stop = threading.Event()
    ran = []

    def run_watch(app, watch, **options):
        ran.append(watch.name)
        stop.set()
        time.sleep(1.5)  # the daemon is shutting down when this run fails
        raise SearchError("offline")  # its retry must not wait for the shutdown

    monkeypatch.setattr("scout.monitor.daemon.run_watch", run_watch)
    timer = threading.Timer(10, stop.set)  # a safety net: never hang the test run
    timer.start()
    try:
        Daemon(app, book).serve(stop)
    finally:
        timer.cancel()
    assert ran == [name]


def test_a_lock_without_waiting_refuses_a_second_holder(tmp_path):
    def lock():
        return FileLock(tmp_path / "daemon.lock", wait=False, busy="already running")

    with lock(), pytest.raises(ConfigError, match="already running"), lock():
        pass
    with lock():  # released: free again
        pass


def test_a_paused_watch_does_not_run_and_success_cancels_the_retry(app, book, monkeypatch):
    book.add(Watch(name="gpu", goal="cheapest RTX 5090", every="6h"))
    outcomes = [SearchError("offline"), None]
    ran = []

    def run_watch(app, watch, **options):
        ran.append(watch.name)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return WatchRun("gpu", 1, SAMPLE_RESULT, (), (), baseline=True)

    monkeypatch.setattr("scout.monitor.daemon.run_watch", run_watch)
    daemon = Daemon(app, book, clock=Clock())
    daemon.run("gpu")
    assert [job.id for job in daemon._scheduler.get_jobs()] == ["gpu:retry"]
    daemon.run("gpu")
    assert daemon._scheduler.get_jobs() == []

    book.update("gpu", paused=True)
    daemon.run("gpu")
    assert ran == ["gpu", "gpu"]
