"""The scheduler: one process runs every active watch on its schedule, one run at a time.

The watches file stays the source of truth: the daemon re-reads it when it changes, so `scout
watch add` or a hand edit takes effect without a restart. A local model answers one request at a
time, so watch runs queue behind each other instead of competing for it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scout.app import App
from scout.clock import utcnow
from scout.errors import AnswerPending, ConfigError, ScoutError
from scout.monitor.runner import notifier_for, run_watch
from scout.monitor.watches import Watch, WatchBook, trigger

log = logging.getLogger(__name__)

RELOAD_SECONDS = 30.0  # how often the watches file is checked for changes
_TICK_SECONDS = 1.0  # how quickly a stop request is noticed
RETRY_AFTER = timedelta(minutes=30)
_PRUNE = "scout:prune"  # a job id no watch can have: names have no colon


class Daemon:
    def __init__(
        self,
        app: App,
        book: WatchBook,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._app = app
        self._book = book
        self._clock = clock
        self._scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(max_workers=1)},
            # A run missed while the computer slept happens once on waking, not once per miss.
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None},
            timezone=UTC,
        )
        self._scheduled: dict[str, Watch] = {}
        self._file_version: int | None = None
        # A run that adds its retry while the scheduler shuts down (holding its job-store lock,
        # waiting for that run) would deadlock: retries are added only before stopping starts.
        self._stopping = False
        self._retry_lock = threading.Lock()

    @property
    def scheduled(self) -> set[str]:
        jobs = self._scheduler.get_jobs()
        return {job.id.removesuffix(":retry") for job in jobs if job.id != _PRUNE}

    def serve(self, stop: threading.Event) -> None:
        """Run watches until *stop* is set."""
        self.sync()
        self._scheduler.add_job(
            self.prune, "interval", days=1, id=_PRUNE, next_run_time=self._clock()
        )
        self._scheduler.start()
        log.info("scheduler started with %d watch(es)", len(self._scheduled))
        last_sync = time.monotonic()
        try:
            while not stop.wait(_TICK_SECONDS):
                if time.monotonic() - last_sync >= RELOAD_SECONDS:
                    self.sync()
                    last_sync = time.monotonic()
        finally:
            with self._retry_lock:
                self._stopping = True
            self._scheduler.shutdown(wait=True)  # a run in progress is finished, not cut off
            log.info("scheduler stopped")

    def sync(self) -> None:
        """Schedule the watches as the file has them now, if it changed since the last look."""
        version = self._book.path.stat().st_mtime_ns if self._book.path.exists() else None
        if version == self._file_version:
            return
        try:
            active = {watch.name: watch for watch in self._book.load() if not watch.paused}
        except Exception as exc:  # a half-saved or mistyped hand edit must not stop the daemon
            log.error("keeping the current schedule; the watches file has a problem: %s", exc)
            return
        self._file_version = version
        for name in self._scheduled.keys() - active.keys():
            for job in (name, f"{name}:retry"):
                if self._scheduler.get_job(job) is not None:
                    self._scheduler.remove_job(job)
            log.info("unscheduled %s", name)
        for name, watch in active.items():
            if self._scheduled.get(name) != watch:
                self._schedule(watch)
        self._scheduled = active

    def prune(self) -> None:
        removed = self._app.store.prune(now=self._clock())
        if removed:
            log.info("pruned %d old cache entries and page versions", removed)

    def run(self, name: str) -> None:
        """One scheduled run. Failures are logged: a broken watch must not stop the others, and
        one that failed for a passing reason (model server down, search throttled) is retried."""
        try:
            watch = self._book.get(name)
            if watch.paused:  # paused after this run was handed to the worker
                return
            outcome = run_watch(self._app, watch, notifier=notifier_for(watch), book=self._book)
        except ConfigError as exc:
            log.error("%s failed: %s", name, exc)
        except AnswerPending as exc:
            log.warning("%s waits for the model's answer: %s", name, exc.request_path)
            self._retry(name)
        except ScoutError as exc:
            log.error("%s failed: %s", name, exc)
            self._retry(name)
        except Exception:
            log.exception("%s failed unexpectedly", name)
        else:
            self._cancel_retry(name)
            changes = ", ".join(f"{n} {c.value}" for c, n in outcome.counts.items()) or "no facts"
            log.info(
                "%s: run %d, %s, %d alert(s)", name, outcome.run_id, changes, len(outcome.alerts)
            )
            if outcome.notify_error:
                log.error("%s: notification failed: %s", name, outcome.notify_error)

    def _retry(self, name: str) -> None:
        when = self._clock() + RETRY_AFTER
        with self._retry_lock:
            if self._stopping:
                return  # a watch whose run failed is due again when the daemon starts
            self._scheduler.add_job(
                self.run,
                "date",
                run_date=when,
                args=[name],
                id=f"{name}:retry",
                replace_existing=True,
            )
        log.info("%s will be retried at %s", name, f"{when:%H:%M} UTC")

    def _cancel_retry(self, name: str) -> None:
        with self._retry_lock:
            if not self._stopping and self._scheduler.get_job(f"{name}:retry") is not None:
                self._scheduler.remove_job(f"{name}:retry")

    def _schedule(self, watch: Watch) -> None:
        options: dict[str, Any] = {}
        due = self._due(watch)
        if due is not None:
            options["next_run_time"] = due
        self._scheduler.add_job(
            self.run,
            trigger(watch),
            args=[watch.name],
            id=watch.name,
            replace_existing=True,
            **options,
        )
        log.info("scheduled %s (%s)", watch.name, watch.every or watch.cron)

    def _due(self, watch: Watch) -> datetime | None:
        """When an interval watch is next due: its interval after its last run, or now if that
        has passed. Cron watches follow their calendar (None)."""
        schedule = trigger(watch)
        if not isinstance(schedule, IntervalTrigger):
            return None
        now = self._clock()
        last = self._app.store.recent_runs(limit=1, watch=watch.name)
        if not last:
            return now
        interval: timedelta = schedule.interval
        return max(now, last[0].started_at + interval)
