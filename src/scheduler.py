from datetime import datetime, timedelta
from typing import Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from database import get_session, Reminder, Thought


_scheduler: AsyncIOScheduler = None
_send_fn: Callable[[str, str, int], Awaitable[None]] = None  # (chat_id, text, reminder_id)


def init_scheduler(db_path: str, send_reminder_fn: Callable):
    global _scheduler, _send_fn
    _send_fn = send_reminder_fn

    jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")}
    _scheduler = AsyncIOScheduler(jobstores=jobstores)
    _scheduler.start()

    _reschedule_all_active()


def _reschedule_all_active():
    """On startup, re-queue any active reminders that survived the reboot."""
    with get_session() as session:
        active = (
            session.query(Reminder)
            .join(Reminder.thought)
            .filter(Reminder.is_active == True)
            .all()
        )
        # Collect data before session closes
        jobs = [
            (r.id, r.thought.chat_id, r.remind_at, r.repeat_hours)
            for r in active
        ]

    for reminder_id, chat_id, remind_at, repeat_hours in jobs:
        _schedule_reminder(reminder_id, chat_id, remind_at, repeat_hours)


def schedule_new_reminder(reminder_id: int, chat_id: str,
                           remind_at: datetime | None, repeat_hours: int):
    _schedule_reminder(reminder_id, chat_id, remind_at, repeat_hours)


def _schedule_reminder(reminder_id: int, chat_id: str,
                        remind_at: datetime | None, repeat_hours: int):
    job_id = f"reminder_{reminder_id}"

    if remind_at:
        _scheduler.add_job(
            _fire_reminder,
            trigger="date",
            run_date=remind_at,
            args=[reminder_id, chat_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=3600,
        )
    else:
        # Recurring: first fire is `repeat_hours` from now, then every `repeat_hours`
        next_run = datetime.utcnow() + timedelta(hours=repeat_hours)
        _scheduler.add_job(
            _fire_reminder,
            trigger="interval",
            hours=repeat_hours,
            next_run_time=next_run,
            args=[reminder_id, chat_id],
            id=job_id,
            replace_existing=True,
        )


async def _fire_reminder(reminder_id: int, chat_id: str):
    content = None
    with get_session() as session:
        reminder = session.get(Reminder, reminder_id)
        if not reminder or not reminder.is_active:
            return

        content = reminder.thought.content
        reminder.last_sent = datetime.utcnow()

        if reminder.remind_at and reminder.repeat_hours == 0:
            reminder.is_active = False

        session.commit()

    await _send_fn(chat_id, content, reminder_id)


def get_next_run_time(reminder_id: int):
    """Returns the next scheduled datetime for a reminder, or None."""
    job = _scheduler.get_job(f"reminder_{reminder_id}")
    return job.next_run_time if job else None


def cancel_reminder(reminder_id: int):
    job_id = f"reminder_{reminder_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    with get_session() as session:
        reminder = session.get(Reminder, reminder_id)
        if reminder:
            reminder.is_active = False
            session.commit()
