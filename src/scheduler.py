"""APScheduler wrapper.

Jobs live in a SQLite job store (same file as the app data), so they
survive restarts with their next-run times intact. All datetimes handed
to this module are naive UTC; the scheduler itself runs in UTC so the
host's local timezone never matters.

This module only touches the job store plus the fire-time bookkeeping on
the Reminder row. Callers own every other DB write and must commit
*before* calling in here — the job store uses its own SQLite connection
and would otherwise block on the caller's open write transaction.
"""

from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

import pytz
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import get_session, Reminder


_scheduler: AsyncIOScheduler = None
_send_fn: Callable[[str, str, int], Awaitable[None]] = None  # (chat_id, text, reminder_id)


def _job_id(reminder_id: int) -> str:
    return f"reminder_{reminder_id}"


def init_scheduler(db_path: str, send_reminder_fn: Callable, paused: bool = False):
    """Start the scheduler. With paused=True jobs can be added, queried
    and removed straight away but nothing fires until resume_scheduler()
    — main() uses this to hold reminders back until the platforms can
    actually send."""
    global _scheduler, _send_fn
    _send_fn = send_reminder_fn

    jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")}
    _scheduler = AsyncIOScheduler(jobstores=jobstores, timezone=pytz.utc)
    _scheduler.start(paused=paused)

    _restore_missing_jobs()


def resume_scheduler():
    _scheduler.resume()


def _restore_missing_jobs():
    """On startup, re-queue active reminders that have no persisted job.

    Jobs that survived in the job store are left alone so a restart does
    not push recurring reminders back by a full interval. This only
    repairs reminders whose job is missing (e.g. a lost job store).
    """
    with get_session() as session:
        active = (
            session.query(Reminder)
            .join(Reminder.thought)
            .filter(Reminder.is_active.is_(True))
            .all()
        )
        jobs = [
            (r.id, r.thought.chat_id, r.remind_at, r.repeat_hours or 0)
            for r in active
        ]

    for reminder_id, chat_id, remind_at, repeat_hours in jobs:
        if _scheduler.get_job(_job_id(reminder_id)) is None:
            schedule_reminder(reminder_id, chat_id, remind_at, repeat_hours)


def schedule_reminder(
    reminder_id: int,
    chat_id: str,
    remind_at: Optional[datetime],
    repeat_hours: int,
    first_run: Optional[datetime] = None,
):
    """Create or replace the job for a reminder.

    repeat_hours > 0 → recurring every `repeat_hours`; the first fire is
    `first_run` if given (used by snooze), else one interval from now.
    Otherwise → one-shot at `remind_at`. A one-shot whose time has
    already passed fires right away rather than being silently dropped.

    misfire_grace_time=None means a job that came due while the bot was
    down still fires (once) when it comes back.
    """
    now = datetime.utcnow()
    common = dict(
        args=[reminder_id, chat_id],
        id=_job_id(reminder_id),
        replace_existing=True,
        misfire_grace_time=None,
        coalesce=True,
    )

    if repeat_hours and repeat_hours > 0:
        _scheduler.add_job(
            _fire_reminder,
            trigger="interval",
            hours=repeat_hours,
            next_run_time=_utc(first_run or now + timedelta(hours=repeat_hours)),
            **common,
        )
    elif remind_at is not None:
        _scheduler.add_job(
            _fire_reminder,
            trigger="date",
            run_date=_utc(max(remind_at, now)),
            **common,
        )


def _utc(dt: datetime) -> datetime:
    return pytz.utc.localize(dt) if dt.tzinfo is None else dt


async def _fire_reminder(reminder_id: int, chat_id: str):
    with get_session() as session:
        reminder = session.get(Reminder, reminder_id)
        if not reminder or not reminder.is_active:
            return

        content = reminder.thought.content
        reminder.last_sent = datetime.utcnow()

        if not reminder.repeat_hours:
            reminder.is_active = False

        session.commit()

    await _send_fn(chat_id, content, reminder_id)


def get_next_run_time(reminder_id: int):
    """Returns the next scheduled datetime for a reminder, or None."""
    job = _scheduler.get_job(_job_id(reminder_id))
    return job.next_run_time if job else None


def remove_job(reminder_id: int):
    """Drop the scheduled job, if any. Does not touch the Reminder row."""
    if _scheduler.get_job(_job_id(reminder_id)):
        _scheduler.remove_job(_job_id(reminder_id))
