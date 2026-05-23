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
from platforms import load_all_platforms
from dispatcher import Dispatcher
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
import secretary as sec_mod
from secretary import (
    InviteError,
    INVITE_TOKEN_PREFIX,
    OwnerContext,
    accept_invite,
    can_act_on_reminder,
    create_invite,
    display_name_for,
    format_actor,
    is_owner,
    leave_workspace,
    list_secretaries,
    refresh_display_name,
    resolve_owner,
    revoke_secretary,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _esc(text: str) -> str:
    return escape(str(text))


cfg = load_config()
# Dispatcher owns the list of active platforms and fans cross-platform
# sends (reminders, owner notifications) out to every platform that can
# reach the user. Set in main().
dispatcher: Dispatcher | None = None

# Tags injected into ForceReply prompts so we can pick the right opp id
# back out of the user's reply without holding conversation state.
FORCEREPLY_TAG_RE = re.compile(r"opp\s+#(\d+)")


def _allowed_set() -> set[int]:
    return set(cfg.telegram.allowed_chat_ids) if cfg.telegram else set()


def _telegram_bot_username() -> str:
    """Read from config if present; otherwise empty (caller falls back).
    Used to construct invite deep-links. We don't fetch from the Bot API
    at call time — that requires an event loop and an instantiated bot.
    Operators can set this in config for accurate links."""
    return getattr(cfg.telegram, "bot_username", "") if cfg.telegram else ""


# ── Top-level message router ─────────────────────────────────────────────


async def on_message(
    chat_id: str,
    text: str,
    reply_to_text: str | None = None,
    user_first_name: str | None = None,
) -> str | dict:
    """Central handler.

    Returns either a string (plain HTML reply) or a dict carrying a
    structured response (CSV upload, opp cards, ForceReply prompt, etc.).
    """
    text = text.strip()
    allowed = _allowed_set()

    # /start with an invite payload is the ONE path that bypasses the
    # owner gate — invitees obviously aren't allowed yet.
    if text.startswith("/start "):
        return await _handle_start_args(chat_id, text[len("/start "):].strip(), user_first_name)

    # Plain /start with no payload from an unknown chat — silent no-op
    # (matches existing _is_allowed behavior of the bot).
    with get_session() as session:
        ctx = resolve_owner(session, chat_id, allowed)
        if ctx is None:
            return ""

        # Cache the first_name if it's a secretary so cards render properly.
        if ctx.is_secretary and user_first_name:
            refresh_display_name(session, chat_id, user_first_name)
            session.commit()

    if text == "/list":
        return _render_list(chat_id, ctx)

    if text == "/whoami":
        return _render_whoami(chat_id, ctx)

    if text == "/members":
        return _render_members(chat_id, ctx)

    if text == "/invite":
        return _handle_invite(chat_id, ctx)

    if text.startswith("/revoke"):
        return _handle_revoke(chat_id, ctx, text[len("/revoke"):].strip())

    if text == "/leave":
        return _handle_leave(chat_id, ctx)

    if text.startswith("/callback "):
        return await _cmd_callback(chat_id, ctx, text[len("/callback "):])

    if text.startswith("/opp"):
        return await _handle_opp(chat_id, ctx, text)

    # ForceReply pickup for opp updates
    if reply_to_text:
        m = FORCEREPLY_TAG_RE.search(reply_to_text)
        if m:
            return await _handle_opp_force_reply(chat_id, ctx, int(m.group(1)), text)

    # Free-text thought capture
    return _handle_thought(chat_id, ctx, text)


# ── /start <invite_token> ────────────────────────────────────────────────


async def _handle_start_args(chat_id: str, args: str, first_name: str | None) -> str:
    if not args.startswith(INVITE_TOKEN_PREFIX):
        return ""  # silent

    if cfg.telegram and getattr(cfg.telegram, "allow_invites", True) is False:
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

    # Notify the owner — fire-and-forget, no need to await on the reply.
    owner_msg = (
        f"✅ <b>{_esc(first_name or 'Someone')} (…{chat_id[-4:]})</b> "
        f"accepted your invite.\n\n"
        f"If this isn't who you meant, run <code>/revoke {chat_id}</code> right away."
    )
    asyncio.create_task(_notify(owner_chat_id, owner_msg))

    return (
        "👋 Welcome! You're now a secretary.\n\n"
        "Anything you type becomes a reminder for your owner. Use /opp commands "
        "to manage their sales pipeline. /whoami to see who you're helping."
    )


async def _notify(chat_id: str, text: str) -> None:
    """Best-effort cross-platform send. Used for owner notifications.
    Each platform that can reach the user gets the message; the
    dispatcher swallows per-platform errors so one failure can't
    suppress the others."""
    if dispatcher is None:
        return
    try:
        await dispatcher.send_html(chat_id, text)
    except Exception as e:
        logger.warning("notify to %s failed: %s", chat_id, e)


# ── /whoami /members /invite /revoke /leave ──────────────────────────────


def _render_whoami(chat_id: str, ctx: OwnerContext) -> str:
    if ctx.is_secretary:
        owner_name = _display_or_id(ctx.owner_chat_id)
        return (
            f"👤 You're a <b>secretary</b> for {owner_name}.\n"
            "Anything you add goes on their list; /leave to step down."
        )
    with get_session() as session:
        secs = list_secretaries(session, ctx.owner_chat_id)
    if not secs:
        return "👤 You're the <b>owner</b>. No secretaries yet — use /invite to add one."
    lines = [f"👤 You're the <b>owner</b>. Active secretaries ({len(secs)}):"]
    for s in secs:
        lines.append(f"  • {_esc(s.display_name or '—')} · <code>{s.secretary_chat_id}</code>")
    return "\n".join(lines)


def _render_members(chat_id: str, ctx: OwnerContext) -> str:
    # Same content as whoami for now — kept as a separate command because
    # users expect /members to exist when they hear "shared bot".
    return _render_whoami(chat_id, ctx)


def _display_or_id(chat_id: str) -> str:
    """Returns a renderable label for a chat_id — display name if known,
    else the bare numeric id."""
    with get_session() as session:
        name = display_name_for(session, chat_id)
    return f"{_esc(name)} (<code>{chat_id}</code>)" if name else f"<code>{chat_id}</code>"


def _handle_invite(chat_id: str, ctx: OwnerContext) -> str:
    if ctx.is_secretary:
        return "❌ Only the owner can invite secretaries."

    if cfg.telegram and getattr(cfg.telegram, "allow_invites", True) is False:
        return "❌ Invites are disabled on this bot."

    if not _allowed_set():
        return "❌ This bot has no configured owner. Add yourself to allowed_chat_ids first."

    max_sec = (
        getattr(cfg.telegram, "max_secretaries_per_owner", 5) if cfg.telegram else 5
    )

    with get_session() as session:
        try:
            invite = create_invite(session, ctx.owner_chat_id, max_secretaries=max_sec)
            session.commit()
            token = invite.token
            expires = invite.expires_at
        except InviteError as e:
            return f"❌ {_esc(str(e))}"

    bot_username = _telegram_bot_username()
    if bot_username:
        link = f"https://t.me/{bot_username}?start={token}"
        link_html = f'<a href="{_esc(link)}">{_esc(link)}</a>'
    else:
        link_html = f"<code>?start={_esc(token)}</code> (set telegram.bot_username in config for a full link)"

    expires_local = pytz.utc.localize(expires).astimezone(pytz.timezone(cfg.timezone))
    return (
        "🔗 <b>Invite link:</b>\n"
        f"{link_html}\n\n"
        f"⏰ Valid until <b>{expires_local.strftime('%Y-%m-%d %H:%M')}</b> · single-use.\n\n"
        "⚠️ Anyone who opens this link within 24h becomes your secretary. "
        "Do not forward, post publicly, or screenshot to channels."
    )


def _handle_revoke(chat_id: str, ctx: OwnerContext, args: str) -> str:
    if ctx.is_secretary:
        return "❌ Only the owner can revoke secretaries."
    target = args.strip()
    if not target.lstrip("-").isdigit():
        return "Usage: <code>/revoke &lt;chat_id&gt;</code>"

    with get_session() as session:
        try:
            sec = revoke_secretary(session, ctx.owner_chat_id, target)
            name = sec.display_name or target
            # Cancel any pending reminders this secretary created.
            cancelled = _cancel_secretary_pending_reminders(session, ctx.owner_chat_id, target)
            session.commit()
        except InviteError as e:
            return f"❌ {_esc(str(e))}"

    tail = f" Cancelled {cancelled} pending reminder(s) they had created." if cancelled else ""
    return f"🗑️ Revoked secretary <b>{_esc(name)}</b>.{tail}"


def _cancel_secretary_pending_reminders(session, owner_chat_id: str, secretary_chat_id: str) -> int:
    """Cancel APScheduler jobs + delete pending reminders the secretary had set."""
    rows = (
        session.query(Thought, Reminder)
        .join(Reminder, Reminder.thought_id == Thought.id)
        .filter(
            Thought.chat_id == owner_chat_id,
            Thought.created_by_chat_id == secretary_chat_id,
            Thought.status == "pending",
            Reminder.is_active == True,
        )
        .all()
    )
    count = 0
    for thought, reminder in rows:
        cancel_reminder(reminder.id)
        session.delete(thought)
        count += 1
    return count


def _handle_leave(chat_id: str, ctx: OwnerContext) -> str:
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

    asyncio.create_task(_notify(
        owner_chat_id, f"👋 <b>{_esc(name)}</b> has left as your secretary."
    ))
    return "👋 You've stepped down. You can no longer add to that owner's list."


# ── /list ────────────────────────────────────────────────────────────────


def _render_list(chat_id: str, ctx: OwnerContext) -> str | dict:
    items = get_pending_items(ctx)
    if not items:
        return "📭 No pending reminders."
    return {
        "kind": "reminder_cards",
        "header": f"📋 <b>{len(items)} pending reminder(s):</b>",
        "items": [
            {
                "reminder_id": it["reminder_id"],
                "text": _format_reminder_card_text(it, ctx),
                "creator_chat_id": it["created_by_chat_id"],
            }
            for it in items
        ],
    }


def _format_reminder_card_text(it: dict, ctx: OwnerContext) -> str:
    if it["next_run"]:
        nr = it["next_run"]
        if nr.tzinfo is None:
            nr = pytz.utc.localize(nr)
        next_str = nr.astimezone(pytz.timezone(cfg.timezone)).strftime("%Y-%m-%d %H:%M")
    else:
        next_str = "—"
    recur = f"every {it['repeat_hours']}h" if it["repeat_hours"] else "once"

    with get_session() as session:
        actor = format_actor(session, it["created_by_chat_id"], ctx.owner_chat_id)
    actor_tag = f" · by {_esc(actor)}" if actor else ""
    return (
        f"📝 {_esc(it['content'])}\n"
        f"<i>{_esc(recur)} · next: {next_str}{actor_tag}</i>"
    )


def get_pending_items(ctx: OwnerContext) -> list[dict]:
    """All pending reminders the actor is allowed to see.

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
                Reminder.is_active == True,
            )
        )
        if ctx.is_secretary:
            q = q.filter(Thought.created_by_chat_id == ctx.actor_chat_id)

        rows = q.order_by(Thought.created_at.desc()).limit(20).all()

        items = []
        for thought, reminder in rows:
            next_run = get_next_run_time(reminder.id)
            items.append({
                "thought_id": thought.id,
                "reminder_id": reminder.id,
                "content": thought.content,
                "repeat_hours": reminder.repeat_hours,
                "next_run": next_run,
                "created_by_chat_id": thought.created_by_chat_id,
            })
        return items


# ── Free-text thought capture ────────────────────────────────────────────


def _handle_thought(chat_id: str, ctx: OwnerContext, text: str) -> str:
    content, remind_at = parse_message(text, cfg.timezone)

    with get_session() as session:
        thought = Thought(
            chat_id=ctx.owner_chat_id,
            created_by_chat_id=ctx.actor_chat_id,
            content=content,
        )
        session.add(thought)
        session.flush()

        if remind_at:
            reminder = Reminder(thought_id=thought.id, remind_at=remind_at, repeat_hours=0)
            tz = pytz.timezone(cfg.timezone)
            local_time = pytz.utc.localize(remind_at).astimezone(tz).strftime('%Y-%m-%d %H:%M')
            base = f"✅ Got it! Reminder at <b>{local_time}</b>."
        else:
            interval = cfg.reminder.default_interval_hours
            reminder = Reminder(thought_id=thought.id, repeat_hours=interval)
            base = f"✅ Got it! Repeats every <b>{interval} hours</b>."

        session.add(reminder)
        session.commit()
        schedule_new_reminder(reminder.id, ctx.owner_chat_id, remind_at, reminder.repeat_hours)

        if ctx.is_secretary:
            owner_label = _display_or_id(ctx.owner_chat_id)
            return f"✅ Added to {owner_label}'s reminders.\n{base}"

    return base


# ── Callback handler ─────────────────────────────────────────────────────


async def _cmd_callback(chat_id: str, ctx: OwnerContext, data: str) -> str | dict:
    parts = data.split(":")

    if parts[0] in ("done", "delete", "snooze"):
        return await _handle_reminder_callback(chat_id, ctx, parts)

    if parts[0].startswith("opp_"):
        return await _handle_opp_callback(chat_id, ctx, parts)

    return "❓ Unknown action."


async def _handle_reminder_callback(chat_id: str, ctx: OwnerContext, parts: list[str]) -> str:
    """done:N, delete:N, snooze:H:N — all gated by creator-or-owner."""
    if parts[0] == "snooze":
        hours = int(parts[1])
        reminder_id = int(parts[2])
    else:
        hours = None
        reminder_id = int(parts[1])

    with get_session() as session:
        reminder = session.get(Reminder, reminder_id)
        if reminder is None:
            return "❌ Reminder not found (already done or deleted)."
        thought = reminder.thought
        if thought.chat_id != ctx.owner_chat_id:
            return "❌ Not your reminder."
        if not can_act_on_reminder(ctx, thought.created_by_chat_id):
            return "❌ You can only act on reminders you created."

    if parts[0] == "done":
        cancel_reminder(reminder_id)
        with get_session() as session:
            r = session.get(Reminder, reminder_id)
            if r:
                r.thought.status = "done"
                session.commit()
        return "✅ Marked as done!"

    if parts[0] == "delete":
        cancel_reminder(reminder_id)
        with get_session() as session:
            r = session.get(Reminder, reminder_id)
            if r:
                session.delete(r.thought)
                session.commit()
        return "🗑️ Deleted."

    if parts[0] == "snooze":
        snooze_until = datetime.utcnow() + timedelta(hours=hours)
        cancel_reminder(reminder_id)
        with get_session() as session:
            old = session.get(Reminder, reminder_id)
            if old is None:
                return "❌ Reminder not found."
            new_reminder = Reminder(
                thought_id=old.thought_id,
                remind_at=snooze_until,
                repeat_hours=0,
            )
            session.add(new_reminder)
            session.commit()
            schedule_new_reminder(new_reminder.id, ctx.owner_chat_id, snooze_until, 0)
        return f"⏰ Snoozed for {hours}h."

    return "❓ Unknown reminder action."


async def _handle_opp_callback(chat_id: str, ctx: OwnerContext, parts: list[str]) -> str | dict:
    action = parts[0]
    opp_id = int(parts[1])

    if action == "opp_advance":
        with get_session() as session:
            opp = get_opp(session, ctx.owner_chat_id, opp_id)
            if not opp:
                return "❌ Opportunity not found (or already deleted)."
            nxt = STAGE_ADVANCE.get(opp.stage)
            if nxt is None:
                return f"⚡ <b>#{opp_id}</b> is already <b>{opp.stage}</b> — no further advance."
            if opp.stage == nxt:   # idempotent guard against double-tap
                return f"⚡ <b>#{opp_id}</b> already at <b>{nxt}</b>."
            change_stage(session, opp, nxt, by_chat_id=ctx.actor_chat_id)
            session.commit()
            return f"▶ <b>#{opp_id}</b> advanced to <b>{nxt}</b>."

    if action == "opp_delete":
        with get_session() as session:
            opp = get_opp(session, ctx.owner_chat_id, opp_id)
            if not opp:
                return "❌ Already deleted."
            soft_delete(session, opp)
            session.commit()
            return f"🗑️ Opportunity <b>#{opp_id}</b> deleted."

    if action == "opp_update":
        with get_session() as session:
            opp = get_opp(session, ctx.owner_chat_id, opp_id)
            if not opp:
                return "❌ Opportunity not found."
            title = opp.title
        return {
            "kind": "force_reply",
            "text": f"📝 Reply to this message with the update note for opp #{opp_id} ({_esc(title)}):",
        }

    return "❓ Unknown opp action."


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
"""


async def _handle_opp(chat_id: str, ctx: OwnerContext, text: str) -> str | dict:
    action = parse_opp_command(text)

    if action.kind == "help":
        return _OPP_HELP

    if action.kind == "invalid":
        return f"❌ {_esc(action.error or 'Bad command. Try /opp help.')}"

    if action.kind == "new":
        with get_session() as session:
            opp = create_opp(session, ctx.owner_chat_id, action.title, action.customer)
            # Record the actor on the implicit "created" update? No — there's
            # no update row for creation in the existing model. We rely on
            # Opportunity.created_at to indicate provenance and tag actor
            # only when they later add notes / change stage.
            session.commit()
            return (
                f"✅ Opportunity <b>#{opp.id}</b> created.\n\n"
                + format_card(opp, cfg.timezone)
            )

    if action.kind == "update":
        with get_session() as session:
            opp = get_opp(session, ctx.owner_chat_id, action.opp_id)
            if not opp:
                return f"❌ Opportunity #{action.opp_id} not found."
            append_update(session, opp, action.note, by_chat_id=ctx.actor_chat_id)
            session.commit()
            return f"📝 Update added to <b>#{opp.id}</b>."

    if action.kind == "stage":
        with get_session() as session:
            opp = get_opp(session, ctx.owner_chat_id, action.opp_id)
            if not opp:
                return f"❌ Opportunity #{action.opp_id} not found."
            if opp.stage == action.stage:
                return f"⚡ <b>#{opp.id}</b> already at <b>{action.stage}</b>."
            change_stage(session, opp, action.stage, by_chat_id=ctx.actor_chat_id)
            session.commit()
            return f"⚡ <b>#{opp.id}</b> stage → <b>{action.stage}</b>."

    if action.kind == "show":
        with get_session() as session:
            opp = get_opp(session, ctx.owner_chat_id, action.opp_id)
            if not opp:
                return f"❌ Opportunity #{action.opp_id} not found."
            return format_show(opp, cfg.timezone)

    if action.kind == "delete":
        with get_session() as session:
            opp = get_opp(session, ctx.owner_chat_id, action.opp_id)
            if not opp:
                return f"❌ Opportunity #{action.opp_id} not found."
            soft_delete(session, opp)
            session.commit()
            return f"🗑️ Opportunity <b>#{opp.id}</b> deleted."

    if action.kind == "list":
        with get_session() as session:
            opps = list_opps(session, ctx.owner_chat_id, action.filter_stage)
            if not opps:
                tail = f" with stage <b>{action.filter_stage}</b>" if action.filter_stage else ""
                return f"📭 No opportunities{tail}."

            if action.list_format == "md":
                md = format_markdown_table(opps, cfg.timezone)
                return f"<pre>{_esc(md)}</pre>"

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
            opps = list_opps(session, ctx.owner_chat_id)
            data = build_csv(opps, cfg.timezone)
            return {
                "kind": "csv",
                "filename": f"opportunities-{datetime.utcnow().strftime('%Y%m%d')}.csv",
                "data": data,
                "caption": f"📎 Exported {len(opps)} opportunit{'y' if len(opps)==1 else 'ies'}.",
            }

    return "❓ Unhandled action."


async def _handle_opp_force_reply(
    chat_id: str,
    ctx: OwnerContext,
    opp_id: int,
    note: str,
) -> str:
    with get_session() as session:
        opp = get_opp(session, ctx.owner_chat_id, opp_id)
        if not opp:
            return f"❌ Opportunity #{opp_id} not found."
        append_update(session, opp, note, by_chat_id=ctx.actor_chat_id)
        session.commit()
        return f"📝 Update added to <b>#{opp_id}</b>."


# ── Scheduler fan-out ────────────────────────────────────────────────────


async def send_reminder(chat_id: str, content: str, reminder_id: int):
    """Called by APScheduler when a reminder fires.

    chat_id is the OWNER's canonical chat_id (we store Thought.chat_id as
    the owner). If the reminder was created by a secretary, we prefix
    the message with "(via <name>)" so the owner knows who added it.

    Fan-out: the dispatcher delivers to every platform that can reach
    the user. A user on both Telegram and Slack gets two pings; the
    buttons on each are independent (clicking Done on Telegram does
    not edit the Slack copy — the duplicate's click is rejected
    idempotently).
    """
    via = ""
    with get_session() as session:
        r = session.get(Reminder, reminder_id)
        if r is not None:
            t = r.thought
            actor = format_actor(session, t.created_by_chat_id, t.chat_id)
            if actor:
                via = f"(via {actor}) "

    if dispatcher is None:
        logger.warning("send_reminder called before dispatcher init")
        return
    await dispatcher.send_reminder(chat_id, f"{via}{content}", reminder_id)


# ── Entry point ──────────────────────────────────────────────────────────


async def main():
    global dispatcher

    db_path = os.environ.get("DB_PATH", "/app/data/reminders.db")
    init_db(db_path)
    init_scheduler(db_path, send_reminder)

    platforms = load_all_platforms(
        cfg, on_message, send_reminder, get_pending_items_owner_only, __version__,
    )
    dispatcher = Dispatcher(platforms)
    logger.info("Starting platforms: %s", [p.name for p in platforms])
    await dispatcher.run()


def get_pending_items_owner_only(chat_id: str) -> list[dict]:
    """Compatibility shim for callers that pass raw chat_id (the platform
    layer). Wraps in the owner-only context."""
    ctx = OwnerContext(owner_chat_id=chat_id, actor_chat_id=chat_id, is_secretary=False)
    return get_pending_items(ctx)


if __name__ == "__main__":
    asyncio.run(main())
