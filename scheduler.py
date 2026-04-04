"""
Job scheduler for Scout using APScheduler with SQLite persistence.

Jobs survive process restarts — they are stored in ~/.scout/jobs.db.
Each job runs the full scout pipeline (search → fetch → LLM → save report).

Usage flow:
  1. `scout schedule "goal" --every 6h` registers the job + blocks
  2. `scout list` reads jobs from DB without needing the scheduler running
  3. `scout stop <id>` removes the job from DB
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED
import re

from exceptions import SchedulerError, JobNotFoundError

logger = logging.getLogger("scout.scheduler")

# ─── TRIGGER PARSING ─────────────────────────────────────────────────────────

_INTERVAL_RE = re.compile(r"^(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)$", re.I)
_UNIT_MAP = {
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "week": "weeks", "weeks": "weeks",
}


def parse_trigger(every: Optional[str], cron: Optional[str]):
    """
    Parse --every or --cron into an APScheduler trigger.

    Interval examples: 30m, 6h, 1d, 1w, 30 minutes, 2 hours
    Cron examples: "0 9 * * *", "*/30 * * * *"
    """
    if cron:
        try:
            return CronTrigger.from_crontab(cron)
        except Exception as e:
            raise SchedulerError(f"Invalid cron expression {cron!r}: {e}") from e

    if every:
        m = _INTERVAL_RE.match(every.strip())
        if not m:
            raise SchedulerError(
                f"Invalid interval {every!r}.\n"
                "Valid examples: 30m, 6h, 1d, 1w, 30min, 2hours"
            )
        val = int(m.group(1))
        unit_key = m.group(2).lower()
        unit = _UNIT_MAP.get(unit_key, "hours")
        if val <= 0:
            raise SchedulerError("Interval must be > 0")
        return IntervalTrigger(**{unit: val})

    raise SchedulerError("Either --every or --cron must be provided")


def describe_trigger(every: Optional[str], cron: Optional[str]) -> str:
    """Human-readable trigger description for display."""
    if cron:
        return f"cron({cron})"
    if every:
        return f"every {every}"
    return "unknown"


# ─── SCHEDULER FACTORY ────────────────────────────────────────────────────────

def _make_scheduler(data_dir: Path) -> BackgroundScheduler:
    """Create a BackgroundScheduler with SQLite persistence."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db_url = f"sqlite:///{data_dir / 'jobs.db'}"
    jobstore = SQLAlchemyJobStore(url=db_url)
    scheduler = BackgroundScheduler(
        jobstores={"default": jobstore},
        job_defaults={
            "coalesce": True,           # merge missed runs into one
            "max_instances": 1,         # don't run same job twice simultaneously
            "misfire_grace_time": 3600, # allow up to 1hr late start
        },
    )
    return scheduler


# ─── JOB FUNCTION ─────────────────────────────────────────────────────────────

def _scout_job(prompt: str, output_dir: str) -> None:
    """
    The actual function APScheduler calls.
    Runs the full scout pipeline: search → fetch → LLM → save report.

    NOTE: All imports are local so APScheduler can pickle the function reference.
    The function must be importable as `scheduler._scout_job` at module level.
    """
    import time as _time

    # Ensure Scouts directory is on path
    scouts_dir = str(Path(__file__).parent)
    if scouts_dir not in sys.path:
        sys.path.insert(0, scouts_dir)

    from config import load_config
    from searcher import gather
    from llm import extract_insights
    from reporter import save_report
    from models import ScoutReport

    start = _time.monotonic()
    logger.info("[job] Starting: %r", prompt)
    print(f"\n[scout] Running scheduled job: {prompt!r}")

    try:
        cfg = load_config()
        pages = gather(prompt, cfg)
        insights = extract_insights(prompt, pages, cfg)

        elapsed = _time.monotonic() - start
        report = ScoutReport(
            goal=prompt,
            insights=insights,
            pages=pages,
            model_used=cfg.lm_studio_model,
            run_duration_seconds=elapsed,
        )

        saved = save_report(report, output_dir)
        print(f"[scout] Report saved: {saved.md_path}")
        logger.info("[job] Done: %r -> %s", prompt, saved.md_path)

    except Exception as e:
        logger.error("[job] Failed: %r — %s", prompt, e)
        print(f"[scout] Job failed: {e}", file=sys.stderr)


# ─── EVENT LISTENERS ──────────────────────────────────────────────────────────

def _make_event_listener():
    def listener(event):
        if event.exception:
            logger.error("Job %s raised an exception: %s", event.job_id, event.exception)
        elif hasattr(event, "missed_run_time"):
            logger.warning("Job %s missed run time: %s", event.job_id, event.missed_run_time)
        else:
            logger.debug("Job %s executed successfully", event.job_id)
    return listener


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def add_job(
    prompt: str,
    output_dir: str,
    every: Optional[str],
    cron: Optional[str],
    data_dir: Path,
) -> str:
    """
    Register a new scheduled job. Returns the job ID (UUID).
    Does NOT start the scheduler loop — call run_scheduler_loop() for that.
    """
    trigger = parse_trigger(every, cron)
    scheduler = _make_scheduler(data_dir)
    scheduler.start()

    try:
        job = scheduler.add_job(
            _scout_job,
            trigger=trigger,
            name=prompt[:80],
            kwargs={"prompt": prompt, "output_dir": str(output_dir)},
            replace_existing=False,
        )
        job_id = job.id
        logger.info("Registered job %s: %r", job_id, prompt)
        return job_id
    finally:
        scheduler.shutdown(wait=False)


def run_scheduler_loop(data_dir: Path) -> None:
    """
    Start the scheduler and block until Ctrl-C.
    Immediately runs any overdue jobs.
    """
    scheduler = _make_scheduler(data_dir)
    scheduler.add_listener(
        _make_event_listener(),
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
    )
    scheduler.start()
    jobs = scheduler.get_jobs()
    logger.info("Scheduler started with %d job(s)", len(jobs))

    try:
        while True:
            time.sleep(15)
    except (KeyboardInterrupt, SystemExit):
        print("\n[scout] Stopping scheduler...")
        scheduler.shutdown(wait=True)
        print("[scout] Scheduler stopped.")


def list_jobs(data_dir: Path) -> list[dict]:
    """
    Return all scheduled jobs as a list of dicts.
    Works even when the scheduler loop is not running.
    """
    scheduler = _make_scheduler(data_dir)
    scheduler.start()
    try:
        jobs = []
        for j in scheduler.get_jobs():
            jobs.append({
                "id": j.id,
                "name": j.name,
                "trigger": str(j.trigger),
                "next_run": str(j.next_run_time) if j.next_run_time else "paused",
                "prompt": j.kwargs.get("prompt", ""),
                "output_dir": j.kwargs.get("output_dir", ""),
            })
        return jobs
    finally:
        scheduler.shutdown(wait=False)


def remove_job(job_id: str, data_dir: Path) -> None:
    """
    Remove a job by full ID.
    Raises JobNotFoundError if not found.
    """
    scheduler = _make_scheduler(data_dir)
    scheduler.start()
    try:
        existing = [j.id for j in scheduler.get_jobs()]
        if job_id not in existing:
            raise JobNotFoundError(f"Job not found: {job_id}")
        scheduler.remove_job(job_id)
        logger.info("Removed job: %s", job_id)
    finally:
        scheduler.shutdown(wait=False)


def pause_job(job_id: str, data_dir: Path) -> None:
    """Pause a job (keeps it in DB but stops it from running)."""
    scheduler = _make_scheduler(data_dir)
    scheduler.start()
    try:
        scheduler.pause_job(job_id)
    except Exception as e:
        raise JobNotFoundError(f"Cannot pause job {job_id}: {e}") from e
    finally:
        scheduler.shutdown(wait=False)


def resume_job(job_id: str, data_dir: Path) -> None:
    """Resume a paused job."""
    scheduler = _make_scheduler(data_dir)
    scheduler.start()
    try:
        scheduler.resume_job(job_id)
    except Exception as e:
        raise JobNotFoundError(f"Cannot resume job {job_id}: {e}") from e
    finally:
        scheduler.shutdown(wait=False)


def resolve_job_id(prefix: str, data_dir: Path) -> str:
    """
    Resolve a short job ID prefix to a full UUID.
    Raises JobNotFoundError if no match or ambiguous.
    """
    jobs = list_jobs(data_dir)
    matches = [j for j in jobs if j["id"].startswith(prefix)]
    if not matches:
        raise JobNotFoundError(f"No job found matching prefix: {prefix!r}")
    if len(matches) > 1:
        ids = ", ".join(j["id"][:8] for j in matches)
        raise SchedulerError(f"Ambiguous prefix {prefix!r} matches: {ids}")
    return matches[0]["id"]
