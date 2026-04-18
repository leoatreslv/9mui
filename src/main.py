import asyncio
import logging
import os
from datetime import datetime, timedelta
from html import escape

import pytz
from config import load_config
from database import init_db, get_session, Thought, Reminder
from scheduler import init_scheduler, schedule_new_reminder, cancel_reminder, get_next_run_time
from time_parser import parse_message
from platforms import load_platform

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _esc(text: str) -> str:
    return escape(str(text))


cfg = load_config()
platform = None  # set after init


async def on_message(chat_id: str, text: str) -> str:
    """Central handler: called by any platform when a message arrives."""

    if text == "/list":
        items = get_pending_items(chat_id)
        if not items:
            return "📭 You have no pending reminders."
        lines = ["📋 <b>Pending reminders:</b>\n"]
        for it in items:
            next_str = it["next_run"].strftime("%Y-%m-%d %H:%M") if it["next_run"] else "—"
            recur = f"every {it['repeat_hours']}h" if it["repeat_hours"] else "once"
            lines.append(f"• {_esc(it['content'])}  <i>{recur}, next: {next_str}</i>")
        return "\n".join(lines)

    if text.startswith("/callback "):
        return await _cmd_callback(chat_id, text[len("/callback "):])

    # Regular thought capture
    content, remind_at = parse_message(text, cfg.timezone)

    with get_session() as session:
        thought = Thought(chat_id=chat_id, content=content)
        session.add(thought)
        session.flush()

        if remind_at:
            reminder = Reminder(thought_id=thought.id, remind_at=remind_at, repeat_hours=0)
            tz = pytz.timezone(cfg.timezone)
            local_time = pytz.utc.localize(remind_at).astimezone(tz).strftime('%Y-%m-%d %H:%M')
            msg = f"✅ Got it! I'll remind you at <b>{local_time}</b>."
        else:
            interval = cfg.reminder.default_interval_hours
            reminder = Reminder(thought_id=thought.id, repeat_hours=interval)
            msg = f"✅ Got it! I'll remind you every <b>{interval} hours</b>."

        session.add(reminder)
        session.commit()
        schedule_new_reminder(reminder.id, chat_id, remind_at, reminder.repeat_hours)

    return msg


def get_pending_items(chat_id: str) -> list[dict]:
    with get_session() as session:
        rows = (
            session.query(Thought, Reminder)
            .join(Reminder, Reminder.thought_id == Thought.id)
            .filter(
                Thought.chat_id == chat_id,
                Thought.status == "pending",
                Reminder.is_active == True,
            )
            .order_by(Thought.created_at.desc())
            .limit(20)
            .all()
        )
        items = []
        for thought, reminder in rows:
            next_run = get_next_run_time(reminder.id)
            items.append({
                "thought_id": thought.id,
                "reminder_id": reminder.id,
                "content": thought.content,
                "repeat_hours": reminder.repeat_hours,
                "next_run": next_run,
            })
        return items


async def _cmd_callback(chat_id: str, data: str) -> str:
    parts = data.split(":")

    if parts[0] == "done":
        reminder_id = int(parts[1])
        cancel_reminder(reminder_id)
        with get_session() as session:
            reminder = session.get(Reminder, reminder_id)
            if reminder:
                reminder.thought.status = "done"
                session.commit()
        return "✅ Marked as done!"

    if parts[0] == "delete":
        reminder_id = int(parts[1])
        cancel_reminder(reminder_id)
        with get_session() as session:
            reminder = session.get(Reminder, reminder_id)
            if reminder:
                session.delete(reminder.thought)
                session.commit()
        return "🗑️ Deleted."

    if parts[0] == "snooze":
        hours = int(parts[1])
        reminder_id = int(parts[2])
        snooze_until = datetime.utcnow() + timedelta(hours=hours)
        cancel_reminder(reminder_id)

        with get_session() as session:
            old = session.get(Reminder, reminder_id)
            if not old:
                return "❌ Reminder not found."
            new_reminder = Reminder(
                thought_id=old.thought_id,
                remind_at=snooze_until,
                repeat_hours=0,
            )
            session.add(new_reminder)
            session.commit()
            schedule_new_reminder(new_reminder.id, chat_id, snooze_until, 0)

        return f"⏰ Snoozed for {hours}h. I'll remind you at <b>{snooze_until.strftime('%H:%M')} UTC</b>."

    return "❓ Unknown action."


async def send_reminder(chat_id: str, content: str, reminder_id: int):
    await platform.send_reminder(chat_id, content, reminder_id)


async def main():
    global platform

    db_path = os.environ.get("DB_PATH", "/app/data/reminders.db")
    init_db(db_path)
    init_scheduler(db_path, send_reminder)

    platform = load_platform(cfg, on_message, send_reminder, get_pending_items)
    await platform.run()


if __name__ == "__main__":
    asyncio.run(main())
