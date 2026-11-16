import asyncio
import logging
from html import escape
from typing import Callable

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# (command, menu description). Every one is routed through on_message.
_COMMANDS = (
    ("list", "Show pending reminders"),
    ("invite", "Invite a secretary"),
    ("members", "Show secretaries / owner"),
    ("revoke", "Revoke a secretary"),
    ("leave", "Step down as secretary"),
    ("whoami", "Show your role"),
    ("help", "Help"),
    ("start", "Welcome"),
)


def _esc(text: str) -> str:
    return escape(str(text))


class TelegramPlatform(BasePlatform):
    name = "telegram"

    def __init__(self, cfg, on_message: Callable):
        self._token = cfg.token
        # Access control lives in the message router (resolve_owner), which
        # considers both allowed_chat_ids and the Secretary table.
        self._on_message = on_message
        self._app = None
        self._ready = asyncio.Event()

    # ── BasePlatform contract ────────────────────────────────────────────

    def identity_for(self, canonical_chat_id: str) -> str | None:
        # Canonical chat_id == Telegram chat_id by v1 invariant.
        # Any numeric string is reachable in principle; bad ids will
        # error at send time, which Telegram itself logs.
        if canonical_chat_id is None or not str(canonical_chat_id).lstrip("-").isdigit():
            return None
        return str(canonical_chat_id)

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

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def run(self) -> None:
        self._app = Application.builder().token(self._token).build()

        for command, _ in _COMMANDS:
            self._app.add_handler(CommandHandler(command, self._handle_text))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        logger.info("Telegram bot starting (polling)...")
        async with self._app:
            await self._app.start()
            self._ready.set()
            await self._app.bot.set_my_commands(
                [BotCommand(command, description) for command, description in _COMMANDS]
            )
            await self._app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            try:
                await asyncio.Event().wait()   # run until cancelled
            finally:
                await self._app.updater.stop()
                await self._app.stop()

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _handle_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Commands and free text alike — the router tells them apart.
        `/start <token>` reaches the router verbatim so an invite can be
        redeemed by someone who isn't allowed yet."""
        if update.message is None:   # e.g. an edited message
            return
        chat_id = str(update.effective_chat.id)
        text = (update.message.text or "").strip()
        first_name = update.effective_user.first_name if update.effective_user else None

        reply = await self._on_message(chat_id, text, first_name)

        if reply == "" and text.split("@")[0] == "/start":
            # Unknown chat without an invite — still say hello so they know
            # the bot is alive and what to do with a link.
            reply = (
                "👋 Hi! I'm 9Mui. If you have an invite link, "
                "tap it instead of typing /start."
            )
        await self._render_reply(update, reply)

    async def _handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        chat_id = str(update.effective_chat.id)
        first_name = update.effective_user.first_name if update.effective_user else None
        await query.answer()

        reply = await self._on_message(chat_id, f"/callback {query.data}", first_name)

        if isinstance(reply, str):
            if reply == "":
                return
            try:
                await query.edit_message_text(reply, parse_mode=_HTML)
            except Exception:
                # The message may be gone or too old to edit. Fall back to
                # a new send so the user still gets feedback.
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

        logger.warning("Unhandled reply kind: %r", kind)
