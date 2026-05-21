import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from html import escape

__version__ = "0.1"

import pytz
from config import load_config
from database import init_db, get_session, Thought, Reminder
from scheduler import init_scheduler, schedule_new_reminder, cancel_reminder, get_next_run_time
from time_parser import parse_message
from platforms import load_platform
from opp import (
    STAGE_ADVANCE,
    parse_opp_command,
    get_opp,
    list_opps,
    create_opp,
    append_update,
    change_stage,
    soft_delete,
    format_card,
    format_show,
    format_markdown_table,
    build_csv,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _esc(text: str) -> str:
    return escape(str(text))


cfg = load_config()
platform = None  # set after init

# ForceReply prompt that tags itself with the opp id, so we can pick up
# the user's next reply without a stateful conversation handler.
FORCEREPLY_TAG_RE = re.compile(r"opp\s+#(\d+)")


async def on_message(chat_id: str, text: str, reply_to_text: str | None = None) -> str | dict:
    """Central handler: called by any platform when a message arrives.

    Return value is either a string (plain reply) or a dict carrying a
    structured action — currently used for sending CSV attachments and
    cards-with-buttons.
    """

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

    if text.startswith("/opp"):
        return await _handle_opp(chat_id, text)

    # Treat as an update reply if the message replies to one of our
    # ForceReply prompts containing "opp #<id>".
    if reply_to_text:
        m = FORCEREPLY_TAG_RE.search(reply_to_text)
        if m:
            return await _handle_opp_force_reply(chat_id, int(m.group(1)), text)

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


async def _cmd_callback(chat_id: str, data: str) -> str | dict:
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

    # Opportunity actions: opp_advance:N, opp_update:N, opp_delete:N
    if parts[0] == "opp_advance":
        opp_id = int(parts[1])
        with get_session() as session:
            opp = get_opp(session, chat_id, opp_id)
            if not opp:
                return "❌ Opportunity not found (or already deleted)."
            nxt = STAGE_ADVANCE.get(opp.stage)
            if nxt is None:
                return f"⚡ <b>#{opp_id}</b> is already <b>{opp.stage}</b> — no further advance."
            change_stage(session, opp, nxt, by_chat_id=chat_id)
            session.commit()
            return f"▶ <b>#{opp_id}</b> advanced to <b>{nxt}</b>."

    if parts[0] == "opp_delete":
        opp_id = int(parts[1])
        with get_session() as session:
            opp = get_opp(session, chat_id, opp_id)
            if not opp:
                return "❌ Already deleted."
            soft_delete(session, opp)
            session.commit()
            return f"🗑️ Opportunity <b>#{opp_id}</b> deleted."

    if parts[0] == "opp_update":
        opp_id = int(parts[1])
        with get_session() as session:
            opp = get_opp(session, chat_id, opp_id)
            if not opp:
                return "❌ Opportunity not found."
            title = opp.title
        return {
            "kind": "force_reply",
            "text": f"📝 Reply to this message with the update note for opp #{opp_id} ({_esc(title)}):",
        }

    return "❓ Unknown action."


# ── Opportunities ────────────────────────────────────────────────────────

_OPP_HELP = """\
<b>Sales opportunities</b>

  /opp new <i>&lt;title&gt;</i> [--customer=<i>&lt;name&gt;</i>]
  /opp update <i>&lt;id&gt;</i> <i>&lt;note&gt;</i>
  /opp stage <i>&lt;id&gt;</i> <i>&lt;lead|qualified|proposal|won|lost&gt;</i>
  /opp show <i>&lt;id&gt;</i>
  /opp list [<i>md</i>] [<i>stage</i>]
  /opp export csv
  /opp delete <i>&lt;id&gt;</i>

<b>Examples</b>
  • <i>/opp new Acme Q1 renewal --customer=Acme Corp</i>
  • <i>/opp update 5 Met with Bob, looking good</i>
  • <i>/opp stage 5 proposal</i>
  • <i>/opp list won</i>
  • <i>/opp list md</i>
"""


async def _handle_opp(chat_id: str, text: str) -> str | dict:
    action = parse_opp_command(text)

    if action.kind == "help":
        return _OPP_HELP

    if action.kind == "invalid":
        return f"❌ {_esc(action.error or 'Bad command. Try /opp help.')}"

    if action.kind == "new":
        with get_session() as session:
            opp = create_opp(session, chat_id, action.title, action.customer)
            session.commit()
            tz = cfg.timezone
            return (
                f"✅ Opportunity <b>#{opp.id}</b> created.\n\n"
                + format_card(opp, tz)
            )

    if action.kind == "update":
        with get_session() as session:
            opp = get_opp(session, chat_id, action.opp_id)
            if not opp:
                return f"❌ Opportunity #{action.opp_id} not found."
            append_update(session, opp, action.note, by_chat_id=chat_id)
            session.commit()
            return f"📝 Update added to <b>#{opp.id}</b>."

    if action.kind == "stage":
        with get_session() as session:
            opp = get_opp(session, chat_id, action.opp_id)
            if not opp:
                return f"❌ Opportunity #{action.opp_id} not found."
            change_stage(session, opp, action.stage, by_chat_id=chat_id)
            session.commit()
            return f"⚡ <b>#{opp.id}</b> stage → <b>{action.stage}</b>."

    if action.kind == "show":
        with get_session() as session:
            opp = get_opp(session, chat_id, action.opp_id)
            if not opp:
                return f"❌ Opportunity #{action.opp_id} not found."
            return format_show(opp, cfg.timezone)

    if action.kind == "delete":
        with get_session() as session:
            opp = get_opp(session, chat_id, action.opp_id)
            if not opp:
                return f"❌ Opportunity #{action.opp_id} not found."
            soft_delete(session, opp)
            session.commit()
            return f"🗑️ Opportunity <b>#{opp.id}</b> deleted."

    if action.kind == "list":
        with get_session() as session:
            opps = list_opps(session, chat_id, action.filter_stage)
            if not opps:
                tail = f" with stage <b>{action.filter_stage}</b>" if action.filter_stage else ""
                return f"📭 No opportunities{tail}."

            if action.list_format == "md":
                md = format_markdown_table(opps, cfg.timezone)
                return f"<pre>{_esc(md)}</pre>"

            # cards: bundle so telegram.py can render inline buttons
            return {
                "kind": "opp_cards",
                "header": f"📊 <b>{len(opps)} opportunit{'y' if len(opps)==1 else 'ies'}</b>"
                          + (f" · stage <b>{action.filter_stage}</b>" if action.filter_stage else ""),
                "items": [
                    {
                        "opp_id": o.id,
                        "text": format_card(o, cfg.timezone),
                        "can_advance": o.stage in STAGE_ADVANCE,
                    }
                    for o in opps
                ],
            }

    if action.kind == "export":
        with get_session() as session:
            opps = list_opps(session, chat_id)
            data = build_csv(opps, cfg.timezone)
            return {
                "kind": "csv",
                "filename": f"opportunities-{datetime.utcnow().strftime('%Y%m%d')}.csv",
                "data": data,
                "caption": f"📎 Exported {len(opps)} opportunit{'y' if len(opps)==1 else 'ies'}.",
            }

    return "❓ Unhandled action."


async def _handle_opp_force_reply(chat_id: str, opp_id: int, note: str) -> str:
    """User replied to a ForceReply prompt tagged with opp #<id>."""
    with get_session() as session:
        opp = get_opp(session, chat_id, opp_id)
        if not opp:
            return f"❌ Opportunity #{opp_id} not found."
        append_update(session, opp, note, by_chat_id=chat_id)
        session.commit()
        return f"📝 Update added to <b>#{opp_id}</b>."


async def send_reminder(chat_id: str, content: str, reminder_id: int):
    await platform.send_reminder(chat_id, content, reminder_id)


async def main():
    global platform

    db_path = os.environ.get("DB_PATH", "/app/data/reminders.db")
    init_db(db_path)
    init_scheduler(db_path, send_reminder)

    platform = load_platform(cfg, on_message, send_reminder, get_pending_items, __version__)
    await platform.run()


if __name__ == "__main__":
    asyncio.run(main())
