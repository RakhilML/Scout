import logging
import os
import threading
import time
from datetime import timedelta

import pytest

from scout.app import App
from scout.errors import ConfigError, SearchError
from scout.files import FileLock
from scout.monitor import runner
from scout.monitor.daemon import RETRY_AFTER, Daemon
from scout.monitor.runner import WatchRun
from scout.monitor.watches import Watch, WatchBook
from scout.settings import Settings
from tests.helpers import NOW, SAMPLE_RESULT, Clock, FakeResearcher


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


def test_a_watch_removed_and_added_again_is_due_at_once(app, book):
    clock = Clock()
    daemon = Daemon(app, book, clock=clock)
    gpu = Watch(name="gpu", goal="g", every="6h")
    app.store.record("gpu", SAMPLE_RESULT, [])  # ran at NOW
    clock.advance(3600)
    assert daemon._due(gpu) == NOW + timedelta(hours=6)

    app.store.forget("gpu")  # removed, then added again
    assert daemon._due(gpu) == clock.now
    assert len(app.store.recent_runs(watch="gpu")) == 1  # its run stays in the history


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


def test_a_watch_removed_and_added_again_unchanged_between_two_looks_runs_at_once(app, book):
    clock = Clock()
    daemon = Daemon(app, book, clock=clock)
    gpu = Watch(name="gpu", goal="g", every="6h")
    book.add(gpu)
    app.store.record("gpu", SAMPLE_RESULT, [])  # ran at NOW
    clock.advance(3600)
    daemon._scheduler.start(paused=True)  # jobs are planned, never run
    try:
        daemon.sync()
        planned = daemon._scheduler.get_job("gpu").next_run_time
        assert planned == NOW + timedelta(hours=6)
        book.add(Watch(name="py", goal="Python news", every="1d"))  # another edit: gpu stays
        touch(book)
        daemon.sync()
        assert daemon._scheduler.get_job("gpu").next_run_time == planned

        book.remove("gpu")  # scout watch remove, then scout watch add, within one look
        app.store.forget("gpu")
        book.add(gpu)
        touch(book)
        daemon.sync()
        assert daemon._scheduler.get_job("gpu").next_run_time == clock.now
    finally:
        daemon._scheduler.shutdown(wait=False)


def test_an_edit_during_a_run_leaves_the_running_watchs_next_run_alone(app, book, monkeypatch):
    clock = Clock()
    daemon = Daemon(app, book, clock=clock)
    book.add(Watch(name="gpu", goal="g", every="6h"))
    app.store.record("gpu", SAMPLE_RESULT, [])  # ran at NOW
    daemon._scheduler.start(paused=True)
    try:
        daemon.sync()
        clock.advance(6 * 3600 + 60)  # its job fired a minute ago and planned the next one
        daemon._scheduler.modify_job("gpu", next_run_time=NOW + timedelta(hours=12))
        planned = []

        def run_watch(app, watch, **options):  # not recorded yet when the file changes
            book.add(Watch(name="py", goal="Python news", every="1d"))
            touch(book)
            daemon.sync()
            planned.append(daemon._scheduler.get_job("gpu").next_run_time)
            raise SearchError("offline")

        monkeypatch.setattr("scout.monitor.daemon.run_watch", run_watch)
        daemon.run("gpu")
        assert planned == [NOW + timedelta(hours=12)]
    finally:
        daemon._scheduler.shutdown(wait=False)


@pytest.mark.parametrize("case", ["failed", "never ran", "queued"])
def test_an_edit_leaves_a_failed_or_waiting_watch_on_its_retry_and_interval(
    app, book, monkeypatch, case
):
    clock = Clock()
    daemon = Daemon(app, book, clock=clock)
    book.add(Watch(name="gpu", goal="g", every="6h"))
    if case != "never ran":
        app.store.record("gpu", SAMPLE_RESULT, [])  # ran at NOW
        clock.advance(6 * 3600)  # due again

    def fail(app, watch, **options):
        raise SearchError("offline")

    monkeypatch.setattr("scout.monitor.daemon.run_watch", fail)
    daemon._scheduler.start(paused=True)  # jobs are planned, never run
    try:
        daemon.sync()
        assert daemon._scheduler.get_job("gpu").next_run_time == clock.now
        later = clock.now + timedelta(hours=6)
        daemon._scheduler.modify_job("gpu", next_run_time=later)  # it fired: next one planned
        if case != "queued":  # queued: it waits for the worker, behind another watch
            daemon.run("gpu")  # fails: retried in 30 minutes
        clock.advance(60)
        book.add(Watch(name="py", goal="Python news", every="1d"))  # scout watch add py
        touch(book)
        daemon.sync()

        assert daemon._scheduler.get_job("gpu").next_run_time == later
        retry = daemon._scheduler.get_job("gpu:retry")
        assert (retry is None) == (case == "queued")
    finally:
        daemon._scheduler.shutdown(wait=False)


def test_a_watch_the_daemon_ran_then_removed_and_added_again_runs_at_once(app, book, monkeypatch):
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(SAMPLE_RESULT))
    monkeypatch.setattr(App, "learn_from", lambda self, researcher: None)
    clock = Clock()
    daemon = Daemon(app, book, clock=clock)
    gpu = Watch(name="gpu", goal="cheapest RTX 5090", every="6h")
    book.add(gpu)
    daemon._scheduler.start(paused=True)
    try:
        daemon.sync()  # no run yet: due at once
        daemon._scheduler.modify_job("gpu", next_run_time=NOW + timedelta(hours=6))  # it fired
        daemon.run("gpu")
        assert app.store.latest_run("gpu") is not None
        clock.advance(3600)
        book.remove("gpu")
        app.store.forget("gpu")
        book.add(gpu)
        touch(book)
        daemon.sync()
        assert daemon._scheduler.get_job("gpu").next_run_time == clock.now
    finally:
        daemon._scheduler.shutdown(wait=False)


def removed_before_the_lock(app: App, book: WatchBook, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove each watch just before its run takes the run lock, as `scout watch remove` can
    after the daemon (or the caller) read the watch."""
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(SAMPLE_RESULT))
    monkeypatch.setattr(App, "learn_from", lambda self, researcher: None)
    lock = runner.run_lock

    def removed_first(data_dir, name, **options):
        book.remove(name)
        app.store.forget(name)
        return lock(data_dir, name, **options)

    monkeypatch.setattr(runner, "run_lock", removed_first)


def test_a_watch_removed_after_the_daemon_read_it_records_nothing(app, book, monkeypatch, caplog):
    book.add(Watch(name="gpu", goal="cheapest RTX 5090", every="6h"))
    removed_before_the_lock(app, book, monkeypatch)
    daemon = Daemon(app, book, clock=Clock())
    with caplog.at_level(logging.ERROR, logger="scout"):
        daemon.run("gpu")

    assert "gpu failed: no watch named 'gpu'" in caplog.text
    assert app.store.recent_runs(watch="gpu") == []
    assert app.store.facts("gpu") == []
    assert not app.settings.feed_path("gpu").exists()
    assert daemon._scheduler.get_jobs() == []  # not retried


def test_a_watch_edited_after_the_caller_read_it_runs_as_it_is_now(app, book, monkeypatch):
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(SAMPLE_RESULT))
    monkeypatch.setattr(App, "learn_from", lambda self, researcher: None)
    read = Watch(name="gpu", goal="cheapest RTX 5090", every="6h", alerts=("new",))
    book.add(read)
    book.update("gpu", alerts=("price below 2000 USD",))
    outcome = runner.run_watch(app, read, book=book, listed_in=book)
    assert [trigger.reason for trigger in outcome.alerts] == [
        "price below 2000 USD: Shop sells it for $1,999"
    ]


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
