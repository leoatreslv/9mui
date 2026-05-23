import asyncio
import io
import logging
from html import escape
from typing import Callable
import pytz

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
    InputFile,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from platforms.base import BasePlatform

logger = logging.getLogger(__name__)
_HTML = "HTML"

_HELP_TEXT = """\
<b>9Mui — Personal Reminder Bot</b>
<i>v{version}</i>

<b>How to use</b>
Send any thought or task — I'll remind you. Or invite secretaries to add things on your behalf.

<b>Commands</b>
  /list      — show pending reminders
  /opp       — sales opportunity tracker (try <i>/opp help</i>)
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


class TelegramPlatform(BasePlatform):
    name = "telegram"

    def __init__(self, cfg, on_message: Callable, send_reminder_fn: Callable,
                 get_pending_items: Callable = None, timezone: str = "UTC",
                 version: str = ""):
        self._token = cfg.token
        # allowed_chat_ids no longer gates everything — the message router
        # uses resolve_owner() which considers both this list and the
        # Secretary table. We still hold a copy for reference.
        self._allowed = set(cfg.allowed_chat_ids)
        self._on_message = on_message
        self._get_pending_items = get_pending_items
        self._tz = pytz.timezone(timezone)
        self._version = version
        self._app = None

    # ── BasePlatform contract + small extension for owner notifications ──

    def identity_for(self, canonical_chat_id: str) -> str | None:
        # Canonical chat_id == Telegram chat_id by v1 invariant.
        # Any numeric string is reachable in principle; bad ids will
        # error at send time, which Telegram itself logs.
        if canonical_chat_id is None or not str(canonical_chat_id).lstrip("-").isdigit():
            return None
        return str(canonical_chat_id)

    async def send_message(self, chat_id: str, text: str) -> None:
        await self._app.bot.send_message(chat_id=chat_id, text=text)

    async def send_html(self, chat_id: str, text: str) -> None:
        """Used by main.py to notify owners on invite-accept / leave."""
        try:
            await self._app.bot.send_message(
                chat_id=int(chat_id), text=text, parse_mode=_HTML
            )
        except Exception as e:
            logger.warning("send_html to %s failed: %s", chat_id, e)

    async def send_reminder(self, chat_id: str, content: str, reminder_id: int) -> None:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Done", callback_data=f"done:{reminder_id}"),
            InlineKeyboardButton("⏰ Snooze 2h", callback_data=f"snooze:2:{reminder_id}"),
            InlineKeyboardButton("⏰ Snooze 8h", callback_data=f"snooze:8:{reminder_id}"),
        ]])
        await self._app.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Reminder:</b> {_esc(content)}",
            parse_mode=_HTML,
            reply_markup=keyboard,
        )

    async def run(self) -> None:
        self._app = Application.builder().token(self._token).build()

        # /start handled specially so we can pass invite tokens to the router.
        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(CommandHandler("help", self._handle_help))
        self._app.add_handler(CommandHandler("list", self._handle_command))
        self._app.add_handler(CommandHandler("opp", self._handle_command))
        self._app.add_handler(CommandHandler("invite", self._handle_command))
        self._app.add_handler(CommandHandler("members", self._handle_command))
        self._app.add_handler(CommandHandler("revoke", self._handle_command))
        self._app.add_handler(CommandHandler("leave", self._handle_command))
        self._app.add_handler(CommandHandler("whoami", self._handle_command))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        logger.info("Telegram bot starting (polling)...")
        async with self._app:
            await self._app.start()
            await self._app.bot.set_my_commands([
                BotCommand("list", "Show pending reminders"),
                BotCommand("opp", "Sales opportunities"),
                BotCommand("invite", "Invite a secretary"),
                BotCommand("members", "Show secretaries / owner"),
                BotCommand("revoke", "Revoke a secretary"),
                BotCommand("leave", "Step down as secretary"),
                BotCommand("whoami", "Show your role"),
                BotCommand("help", "Help"),
                BotCommand("start", "Welcome"),
            ])
            await self._app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            await asyncio.get_event_loop().create_future()

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _handle_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/start may carry an invite token. Always route through main —
        a token-bearing /start must bypass any access gate."""
        chat_id = str(update.effective_chat.id)
        first_name = update.effective_user.first_name if update.effective_user else None
        args = update.message.text or "/start"

        # When /start has no payload, send the welcome (after permission check).
        if args.strip() in ("/start", "/start@"):
            reply = await self._on_message(chat_id, "", None, first_name)
            if reply == "":
                # Unknown chat without an invite — still show a welcome so the
                # user can paste a token if they have one.
                await update.message.reply_text(
                    "👋 Hi! I'm 9Mui. If you have an invite link, tap it instead of typing /start.\n"
                    "Otherwise, /help.",
                    parse_mode=_HTML,
                )
                return
            await self._render_reply(update, reply)
            return

        reply = await self._on_message(chat_id, args, None, first_name)
        await self._render_reply(update, reply)

    async def _handle_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            _HELP_TEXT.format(version=self._version or "—"),
            parse_mode=_HTML,
        )

    async def _handle_command(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self._handle_text(update, ctx)

    async def _handle_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        text = (update.message.text or "").strip()
        first_name = update.effective_user.first_name if update.effective_user else None

        reply_to_text = None
        if update.message.reply_to_message and update.message.reply_to_message.text:
            reply_to_text = update.message.reply_to_message.text

        reply = await self._on_message(chat_id, text, reply_to_text, first_name)
        await self._render_reply(update, reply)

    async def _handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        chat_id = str(update.effective_chat.id)
        first_name = update.effective_user.first_name if update.effective_user else None
        await query.answer()

        reply = await self._on_message(chat_id, f"/callback {query.data}", None, first_name)

        if isinstance(reply, dict) and reply.get("kind") == "force_reply":
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=reply["text"],
                parse_mode=_HTML,
                reply_markup=ForceReply(selective=True),
            )
            return

        if isinstance(reply, str):
            if reply == "":
                return
            try:
                await query.edit_message_text(reply, parse_mode=_HTML)
            except Exception:
                # The message may have been deleted (e.g. after /opp delete).
                # Fall back to a new send so the user gets feedback.
                await self._app.bot.send_message(chat_id=chat_id, text=reply, parse_mode=_HTML)
            return

        await self._render_reply_to_chat(chat_id, reply)

    # ── Reply rendering ──────────────────────────────────────────────────

    async def _render_reply(self, update: Update, reply) -> None:
        if reply is None or reply == "":
            return
        if isinstance(reply, str):
            await update.message.reply_text(reply, parse_mode=_HTML, disable_web_page_preview=True)
            return
        await self._render_reply_to_chat(str(update.effective_chat.id), reply)

    async def _render_reply_to_chat(self, chat_id: str, reply) -> None:
        if not isinstance(reply, dict):
            return

        kind = reply.get("kind")

        if kind == "reminder_cards":
            if reply.get("header"):
                await self._app.bot.send_message(
                    chat_id=chat_id, text=reply["header"], parse_mode=_HTML)
            for item in reply["items"]:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Done", callback_data=f"done:{item['reminder_id']}"),
                    InlineKeyboardButton("🗑️ Delete", callback_data=f"delete:{item['reminder_id']}"),
                ]])
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=item["text"],
                    parse_mode=_HTML,
                    reply_markup=keyboard,
                )
            return

        if kind == "opp_cards":
            if reply.get("header"):
                await self._app.bot.send_message(
                    chat_id=chat_id, text=reply["header"], parse_mode=_HTML)
            for item in reply["items"]:
                buttons = [
                    InlineKeyboardButton("📝 Update", callback_data=f"opp_update:{item['opp_id']}"),
                ]
                if item.get("can_advance"):
                    buttons.append(InlineKeyboardButton("▶ Advance", callback_data=f"opp_advance:{item['opp_id']}"))
                buttons.append(InlineKeyboardButton("🗑️ Delete", callback_data=f"opp_delete:{item['opp_id']}"))
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=item["text"],
                    parse_mode=_HTML,
                    reply_markup=InlineKeyboardMarkup([buttons]),
                )
            return

        if kind == "csv":
            doc = InputFile(io.BytesIO(reply["data"]), filename=reply["filename"])
            await self._app.bot.send_document(
                chat_id=chat_id,
                document=doc,
                caption=reply.get("caption", ""),
                parse_mode=_HTML,
            )
            return

        if kind == "force_reply":
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=reply["text"],
                parse_mode=_HTML,
                reply_markup=ForceReply(selective=True),
            )
            return

        logger.warning("Unhandled reply kind: %r", kind)
