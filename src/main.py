import asyncio
import logging
import os
from datetime import datetime, timedelta
from html import escape

__version__ = "0.2"

import pytz
from config import load_config
from database import init_db, get_session, Thought, Reminder
from scheduler import (
    init_scheduler,
    resume_scheduler,
    schedule_reminder,
    remove_job,
    get_next_run_time,
)
from time_parser import parse_message
from platforms import load_all_platforms
from dispatcher import Dispatcher
from secretary import (
    InviteError,
    INVITE_TOKEN_PREFIX,
    OwnerContext,
    accept_invite,
    can_act_on_reminder,
    create_invite,
    display_name_for,
    format_actor,
    leave_workspace,
    list_secretaries,
    resolve_owner,
    revoke_secretary,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# /list shows at most this many reminders.
LIST_LIMIT = 20
# Upper bound accepted from a snooze button payload.
MAX_SNOOZE_HOURS = 24 * 7

HELP_TEXT = """\
<b>9Mui — Personal Reminder Bot</b>
<i>v{version}</i>

<b>How to use</b>
Send any thought or task — I'll remind you. Or invite secretaries to add things on your behalf.

<b>Commands</b>
  /list      — show pending reminders
  /invite    — invite a secretary (owner only)
  /members   — list secretaries / show your owner
  /revoke    — revoke a secretary (owner only)
  /leave     — step down as a secretary
  /whoami    — show your role
  /help      — show this message

<b>Reminder actions</b>
  ✅ Done · ⏰ Snooze 2h / 8h · 🗑️ Delete

<b>Time expressions</b>
  • <i>at 4pm</i>, <i>by 3pm</i>, <i>in 2 hours</i>
  • <i>tomorrow 9am</i>, <i>next Monday</i>
  • <i>明天下午4點</i>, <i>5分鐘後</i>
"""


def _esc(text: str) -> str:
    return escape(str(text))


cfg = load_config()
# Dispatcher owns the list of active platforms and fans cross-platform
# sends (reminders, owner notifications) out to every platform that can
# reach the user. Set in main().
dispatcher: Dispatcher | None = None


def _allowed_set() -> set[int]:
    return set(cfg.telegram.allowed_chat_ids) if cfg.telegram else set()


def _invites_enabled() -> bool:
    return cfg.telegram.allow_invites if cfg.telegram else True


def _local_str(dt: datetime) -> str:
    """Format a naive-UTC (or aware) datetime in the configured timezone."""
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(pytz.timezone(cfg.timezone)).strftime("%Y-%m-%d %H:%M")


# ── Top-level message router ─────────────────────────────────────────────


def _split_command(text: str) -> tuple[str | None, str]:
    """"/List@my_bot  foo" → ("/list", "foo"); free text → (None, text)."""
    if not text.startswith("/"):
        return None, text
    parts = text.split(None, 1)
    cmd = parts[0].split("@", 1)[0].lower()
    return cmd, parts[1].strip() if len(parts) > 1 else ""


async def on_message(
    chat_id: str,
    text: str,
    user_first_name: str | None = None,
) -> str | dict:
    """Central handler, shared by every platform.

    Returns either a string (plain HTML reply; "" means stay silent) or
    a dict carrying a structured response (currently only
    {"kind": "reminder_cards", ...}).
    """
    text = (text or "").strip()
    cmd, args = _split_command(text)

    # /start with an invite payload is the ONE path that bypasses the
    # owner gate — invitees obviously aren't allowed yet.
    if cmd == "/start" and args:
        return await _handle_start_args(chat_id, args, user_first_name)

    with get_session() as session:
        ctx = resolve_owner(session, chat_id, _allowed_set())
        if ctx is None:
            return ""  # unknown chat — silent

        # Cache the first_name of a secretary so cards render properly.
        sec = ctx.secretary
        if sec is not None and user_first_name and sec.display_name != user_first_name:
            sec.display_name = user_first_name
            session.commit()

    if cmd is None:
        return _handle_thought(ctx, text)

    if cmd == "/callback":
        return _handle_callback(ctx, args)

    if cmd in ("/start", "/help"):
        return HELP_TEXT.format(version=__version__)
    if cmd == "/list":
        return _render_list(ctx)
    if cmd in ("/whoami", "/members"):
        return _render_whoami(ctx)
    if cmd == "/invite":
        return _handle_invite(ctx)
    if cmd == "/revoke":
        return _handle_revoke(ctx, args)
    if cmd == "/leave":
        return await _handle_leave(ctx)

    # Never store an unrecognised command as a reminder.
    return "❓ Unknown command. Try /help."


# ── /start <invite_token> ────────────────────────────────────────────────


async def _handle_start_args(chat_id: str, args: str, first_name: str | None) -> str:
    if not args.startswith(INVITE_TOKEN_PREFIX):
        return ""  # silent

    if not _invites_enabled():
        return "❌ Invites are disabled on this bot."

    with get_session() as session:
        try:
            sec = accept_invite(
                session=session,
                token=args,
                secretary_chat_id=chat_id,
                secretary_display_name=first_name,
                allowed_chat_ids=_allowed_set(),
            )
            owner_chat_id = sec.owner_chat_id
            session.commit()
        except InviteError as e:
            return f"❌ {_esc(str(e))}"

    await _notify(
        owner_chat_id,
        f"✅ <b>{_esc(first_name or 'Someone')} (…{chat_id[-4:]})</b> "
        f"accepted your invite.\n\n"
        f"If this isn't who you meant, run <code>/revoke {chat_id}</code> right away.",
    )

    return (
        "👋 Welcome! You're now a secretary.\n\n"
        "Anything you type becomes a reminder for your owner. "
        "/whoami to see who you're helping."
    )


async def _notify(chat_id: str, text: str) -> None:
    """Best-effort cross-platform send. Used for owner notifications.
    Each platform that can reach the user gets the message; failures are
    logged and never propagate to the caller."""
    if dispatcher is None:
        return
    try:
        await dispatcher.send_html(chat_id, text)
    except Exception as e:
        logger.warning("notify to %s failed: %s", chat_id, e)


# ── /whoami /members /invite /revoke /leave ──────────────────────────────


def _render_whoami(ctx: OwnerContext) -> str:
    """Also serves /members — same content for now."""
    with get_session() as session:
        if ctx.is_secretary:
            owner_name = _owner_label(session, ctx.owner_chat_id)
            return (
                f"👤 You're a <b>secretary</b> for {owner_name}.\n"
                "Anything you add goes on their list; /leave to step down."
            )
        secs = list_secretaries(session, ctx.owner_chat_id)
        if not secs:
            return "👤 You're the <b>owner</b>. No secretaries yet — use /invite to add one."
        lines = [f"👤 You're the <b>owner</b>. Active secretaries ({len(secs)}):"]
        for s in secs:
            lines.append(f"  • {_esc(s.display_name or '—')} · <code>{s.secretary_chat_id}</code>")
        return "\n".join(lines)


def _owner_label(session, chat_id: str) -> str:
    """Renderable label for a chat_id — display name if known, else the
    bare numeric id."""
    name = display_name_for(session, chat_id)
    return f"{_esc(name)} (<code>{chat_id}</code>)" if name else f"<code>{chat_id}</code>"


def _handle_invite(ctx: OwnerContext) -> str:
    if ctx.is_secretary:
        return "❌ Only the owner can invite secretaries."

    if not _invites_enabled():
        return "❌ Invites are disabled on this bot."

    if not _allowed_set():
        return "❌ This bot has no configured owner. Add yourself to allowed_chat_ids first."

    max_sec = cfg.telegram.max_secretaries_per_owner if cfg.telegram else 5

    with get_session() as session:
        try:
            invite = create_invite(session, ctx.owner_chat_id, max_secretaries=max_sec)
            session.commit()
            token = invite.token
            expires = invite.expires_at
        except InviteError as e:
            return f"❌ {_esc(str(e))}"

    bot_username = cfg.telegram.bot_username if cfg.telegram else ""
    if bot_username:
        link = f"https://t.me/{bot_username}?start={token}"
        link_html = f'<a href="{_esc(link)}">{_esc(link)}</a>'
    else:
        link_html = f"<code>?start={_esc(token)}</code> (set telegram.bot_username in config for a full link)"

    return (
        "🔗 <b>Invite link:</b>\n"
        f"{link_html}\n\n"
        f"⏰ Valid until <b>{_local_str(expires)}</b> · single-use.\n\n"
        "⚠️ Anyone who opens this link within 24h becomes your secretary. "
        "Do not forward, post publicly, or screenshot to channels."
    )


def _handle_revoke(ctx: OwnerContext, args: str) -> str:
    if ctx.is_secretary:
        return "❌ Only the owner can revoke secretaries."
    target = args.strip()
    if not target.lstrip("-").isdigit():
        return "Usage: <code>/revoke &lt;chat_id&gt;</code>"

    with get_session() as session:
        try:
            sec = revoke_secretary(session, ctx.owner_chat_id, target)
        except InviteError as e:
            return f"❌ {_esc(str(e))}"
        name = sec.display_name or target
        cancelled_ids = _cancel_secretary_pending_reminders(session, ctx.owner_chat_id, target)
        session.commit()

    # Job store has its own connection — touch it only after our commit.
    for reminder_id in cancelled_ids:
        remove_job(reminder_id)

    tail = (
        f" Cancelled {len(cancelled_ids)} pending reminder(s) they had created."
        if cancelled_ids else ""
    )
    return f"🗑️ Revoked secretary <b>{_esc(name)}</b>.{tail}"


def _cancel_secretary_pending_reminders(
    session, owner_chat_id: str, secretary_chat_id: str
) -> list[int]:
    """Cancel (not delete) the still-active reminders a secretary set.

    The thought rows stay, marked "cancelled", so the owner's history is
    preserved. Returns the reminder ids whose jobs the caller must remove
    after committing.
    """
    rows = (
        session.query(Thought, Reminder)
        .join(Reminder, Reminder.thought_id == Thought.id)
        .filter(
            Thought.chat_id == owner_chat_id,
            Thought.created_by_chat_id == secretary_chat_id,
            Thought.status == "pending",
            Reminder.is_active.is_(True),
        )
        .all()
    )
    ids = []
    for thought, reminder in rows:
        thought.status = "cancelled"
        reminder.is_active = False
        ids.append(reminder.id)
    return ids


async def _handle_leave(ctx: OwnerContext) -> str:
    if not ctx.is_secretary:
        return "❌ You're the owner — there's nothing to leave."
    with get_session() as session:
        try:
            sec = leave_workspace(session, ctx.actor_chat_id)
            owner_chat_id = sec.owner_chat_id
            name = sec.display_name or ctx.actor_chat_id
            session.commit()
        except InviteError as e:
            return f"❌ {_esc(str(e))}"

    await _notify(owner_chat_id, f"👋 <b>{_esc(name)}</b> has left as your secretary.")
    return "👋 You've stepped down. You can no longer add to that owner's list."


# ── /list ────────────────────────────────────────────────────────────────


def _render_list(ctx: OwnerContext) -> str | dict:
    items = get_pending_items(ctx)
    if not items:
        return "📭 No pending reminders."
    header = f"📋 <b>{len(items)} pending reminder(s):</b>"
    if len(items) == LIST_LIMIT:
        header = f"📋 <b>Latest {LIST_LIMIT} pending reminders:</b>"
    return {
        "kind": "reminder_cards",
        "header": header,
        "items": [
            {"reminder_id": it["reminder_id"], "text": _format_reminder_card_text(it)}
            for it in items
        ],
    }


def _format_reminder_card_text(it: dict) -> str:
    next_str = _local_str(it["next_run"]) if it["next_run"] else "—"
    recur = f"every {it['repeat_hours']}h" if it["repeat_hours"] else "once"
    actor_tag = f" · by {_esc(it['actor'])}" if it["actor"] else ""
    return (
        f"📝 {_esc(it['content'])}\n"
        f"<i>{_esc(recur)} · next: {next_str}{actor_tag}</i>"
    )


def get_pending_items(ctx: OwnerContext) -> list[dict]:
    """Pending reminders the actor is allowed to see, newest first.

    Owner: everything on their list. Secretary: only what they themselves
    created. The filter is applied at the DB layer so secretaries can never
    accidentally render someone else's row.
    """
    with get_session() as session:
        q = (
            session.query(Thought, Reminder)
            .join(Reminder, Reminder.thought_id == Thought.id)
            .filter(
                Thought.chat_id == ctx.owner_chat_id,
                Thought.status == "pending",
                Reminder.is_active.is_(True),
            )
        )
        if ctx.is_secretary:
            q = q.filter(Thought.created_by_chat_id == ctx.actor_chat_id)

        rows = q.order_by(Thought.created_at.desc()).limit(LIST_LIMIT).all()

        # One lookup per distinct creator rather than one per row.
        actors: dict[str | None, str] = {}
        items = []
        for thought, reminder in rows:
            creator = thought.created_by_chat_id
            if creator not in actors:
                actors[creator] = format_actor(session, creator, ctx.owner_chat_id)
            items.append({
                "thought_id": thought.id,
                "reminder_id": reminder.id,
                "content": thought.content,
                "repeat_hours": reminder.repeat_hours,
                "next_run": get_next_run_time(reminder.id),
                "created_by_chat_id": creator,
                "actor": actors[creator],
            })
        return items


# ── Free-text thought capture ────────────────────────────────────────────


def _handle_thought(ctx: OwnerContext, text: str) -> str:
    if not text:
        return ""

    content, remind_at = parse_message(text, cfg.timezone)
    content = content or text

    with get_session() as session:
        thought = Thought(
            chat_id=ctx.owner_chat_id,
            created_by_chat_id=ctx.actor_chat_id,
            content=content,
        )
        if remind_at:
            reminder = Reminder(remind_at=remind_at, repeat_hours=0)
            base = f"✅ Got it! Reminder at <b>{_local_str(remind_at)}</b>."
        else:
            interval = cfg.reminder.default_interval_hours
            reminder = Reminder(repeat_hours=interval)
            base = f"✅ Got it! Repeats every <b>{interval} hours</b>."
        thought.reminders.append(reminder)
        session.add(thought)
        session.commit()

        reminder_id, repeat_hours = reminder.id, reminder.repeat_hours
        owner_label = _owner_label(session, ctx.owner_chat_id) if ctx.is_secretary else None

    schedule_reminder(reminder_id, ctx.owner_chat_id, remind_at, repeat_hours)

    if owner_label:
        return f"✅ Added to {owner_label}'s reminders.\n{base}"
    return base


# ── Callback handler (inline buttons) ────────────────────────────────────


def _handle_callback(ctx: OwnerContext, data: str) -> str:
    """done:N, delete:N, snooze:H:N — all gated by creator-or-owner."""
    parts = data.split(":")
    action = parts[0]
    try:
        if action in ("done", "delete") and len(parts) == 2:
            hours, reminder_id = None, int(parts[1])
        elif action == "snooze" and len(parts) == 3:
            hours, reminder_id = int(parts[1]), int(parts[2])
            if not 0 < hours <= MAX_SNOOZE_HOURS:
                raise ValueError(hours)
        else:
            return "❓ Unknown action."
    except ValueError:
        return "❓ Unknown action."

    stale_jobs: list[int] = []   # jobs to drop once our transaction is committed
    snooze = None                # (remind_at, repeat_hours, first_run)

    with get_session() as session:
        reminder = session.get(Reminder, reminder_id)
        if reminder is None:
            return "❌ Reminder not found (already done or deleted)."
        thought = reminder.thought
        if thought.chat_id != ctx.owner_chat_id:
            return "❌ Not your reminder."
        if not can_act_on_reminder(ctx, thought.created_by_chat_id):
            return "❌ You can only act on reminders you created."

        if action == "delete":
            stale_jobs = [r.id for r in thought.reminders]
            session.delete(thought)
            reply = "🗑️ Deleted."

        elif action == "done":
            thought.status = "done"
            for r in thought.reminders:
                r.is_active = False
                stale_jobs.append(r.id)
            reply = "✅ Marked as done!"

        else:  # snooze
            if thought.status != "pending":
                # e.g. a stale button on another platform's copy of the card.
                return "❌ This reminder is already closed."
            until = datetime.utcnow() + timedelta(hours=hours)
            # Older releases created a fresh row per snooze; make sure only
            # the row being snoozed stays live so the user isn't pinged twice.
            for r in thought.reminders:
                if r.id != reminder.id and r.is_active:
                    r.is_active = False
                    stale_jobs.append(r.id)
            reminder.is_active = True
            if not reminder.repeat_hours:
                reminder.remind_at = until
            # A recurring reminder keeps its interval; only the next fire moves.
            snooze = (reminder.remind_at, reminder.repeat_hours or 0, until)
            reply = f"⏰ Snoozed for {hours}h."

        session.commit()

    for rid in stale_jobs:
        remove_job(rid)
    if snooze:
        remind_at, repeat_hours, first_run = snooze
        schedule_reminder(reminder_id, ctx.owner_chat_id, remind_at, repeat_hours,
                          first_run=first_run)
    return reply


# ── Scheduler fan-out ────────────────────────────────────────────────────


async def send_reminder(chat_id: str, content: str, reminder_id: int):
    """Called by APScheduler when a reminder fires.

    chat_id is the OWNER's canonical chat_id (we store Thought.chat_id as
    the owner). If the reminder was created by a secretary, we prefix
    the message with "(via <name>)" so the owner knows who added it.

    Fan-out: the dispatcher delivers to every platform that can reach
    the user. A user on both Telegram and Slack gets two pings; the
    buttons on each are independent (clicking Done on Telegram does
    not edit the Slack copy).
    """
    if dispatcher is None:
        logger.warning("send_reminder called before dispatcher init")
        return

    via = ""
    with get_session() as session:
        r = session.get(Reminder, reminder_id)
        if r is not None:
            t = r.thought
            actor = format_actor(session, t.created_by_chat_id, t.chat_id)
            if actor:
                via = f"(via {actor}) "

    await dispatcher.send_reminder(chat_id, f"{via}{content}", reminder_id)


# ── Entry point ──────────────────────────────────────────────────────────


async def main():
    global dispatcher

    db_path = os.environ.get("DB_PATH", "/app/data/reminders.db")
    init_db(db_path)

    # The scheduler accepts jobs from the start (platforms begin taking
    # messages as soon as each one is up) but fires nothing until every
    # platform can send, so a reminder that came due while the bot was
    # down has somewhere to go.
    init_scheduler(db_path, send_reminder, paused=True)

    platforms = load_all_platforms(cfg, on_message)
    dispatcher = Dispatcher(platforms)
    logger.info("Starting platforms: %s", [p.name for p in platforms])
    runner = asyncio.create_task(dispatcher.run())
    await dispatcher.wait_ready()
    resume_scheduler()
    await runner


if __name__ == "__main__":
    asyncio.run(main())
