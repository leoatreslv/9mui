"""Scheduler behaviour across a restart, against a real job store."""

from datetime import datetime, timedelta

import database
import scheduler
from database import Reminder, Thought


def _boot(db_path, sent):
    async def send(chat_id, content, reminder_id):
        sent.append((chat_id, content, reminder_id))

    database.init_db(db_path)
    scheduler.init_scheduler(db_path, send)


def _shutdown():
    scheduler._scheduler.shutdown(wait=False)
    database._engine.dispose()


def _add(remind_at=None, repeat_hours=0):
    with database.get_session() as s:
        t = Thought(chat_id="111", created_by_chat_id="111", content="x")
        r = Reminder(remind_at=remind_at, repeat_hours=repeat_hours)
        t.reminders.append(r)
        s.add(t)
        s.commit()
        return r.id


async def test_restart_keeps_recurring_schedule(tmp_path):
    db = str(tmp_path / "r.db")
    _boot(db, [])
    rid = _add(repeat_hours=8)
    first = datetime.utcnow() + timedelta(hours=3)
    scheduler.schedule_reminder(rid, "111", None, 8, first_run=first)
    before = scheduler.get_next_run_time(rid)
    _shutdown()

    _boot(db, [])
    try:
        assert scheduler.get_next_run_time(rid) == before   # not pushed out to +8h
    finally:
        _shutdown()


async def test_restart_restores_missing_jobs(tmp_path):
    db = str(tmp_path / "r.db")
    _boot(db, [])
    recurring = _add(repeat_hours=8)
    one_shot = _add(remind_at=datetime.utcnow() + timedelta(hours=1))
    _shutdown()                        # never scheduled → no persisted jobs

    _boot(db, [])
    try:
        assert scheduler.get_next_run_time(recurring) is not None
        assert scheduler.get_next_run_time(one_shot) is not None
    finally:
        _shutdown()


async def test_fire_deactivates_one_shot_only(tmp_path):
    sent = []
    _boot(str(tmp_path / "r.db"), sent)
    try:
        recurring = _add(repeat_hours=8)
        one_shot = _add(remind_at=datetime.utcnow() + timedelta(hours=1))
        await scheduler._fire_reminder(recurring, "111")
        await scheduler._fire_reminder(one_shot, "111")
        await scheduler._fire_reminder(one_shot, "111")     # inactive → no resend
        assert [s[2] for s in sent] == [recurring, one_shot]
        with database.get_session() as s:
            assert s.get(Reminder, recurring).is_active is True
            assert s.get(Reminder, one_shot).is_active is False
    finally:
        _shutdown()


async def test_run_times_are_utc_regardless_of_host_timezone(tmp_path):
    _boot(str(tmp_path / "r.db"), [])
    try:
        when = datetime.utcnow().replace(microsecond=0) + timedelta(hours=1)
        rid = _add(remind_at=when)
        scheduler.schedule_reminder(rid, "111", when, 0)
        nxt = scheduler.get_next_run_time(rid)
        assert nxt.utcoffset() == timedelta(0)
        assert nxt.replace(tzinfo=None) == when
    finally:
        _shutdown()


async def test_paused_scheduler_accepts_jobs_but_fires_only_after_resume(tmp_path):
    import asyncio

    sent = []

    async def send(chat_id, content, reminder_id):
        sent.append(reminder_id)

    database.init_db(str(tmp_path / "r.db"))
    scheduler.init_scheduler(str(tmp_path / "r.db"), send, paused=True)
    try:
        rid = _add(remind_at=datetime.utcnow() - timedelta(hours=5))   # overdue
        scheduler.schedule_reminder(rid, "111", datetime.utcnow() - timedelta(hours=5), 0)
        assert scheduler.get_next_run_time(rid) is not None            # usable while paused
        await asyncio.sleep(0.3)
        assert sent == []

        scheduler.resume_scheduler()
        await asyncio.sleep(0.3)
        assert sent == [rid]
    finally:
        _shutdown()
