import asyncio
import logging
from html import escape
from typing import Callable
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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


def _esc(text: str) -> str:
    return escape(str(text))


class TelegramPlatform(BasePlatform):
    def __init__(self, cfg, on_message: Callable, send_reminder_fn: Callable,
                 get_pending_items: Callable = None, timezone: str = "UTC"):
        self._token = cfg.token
        self._allowed = set(cfg.allowed_chat_ids)
        self._on_message = on_message
        self._get_pending_items = get_pending_items
        self._tz = pytz.timezone(timezone)
        self._app = None

    def _is_allowed(self, chat_id: str) -> bool:
        return not self._allowed or int(chat_id) in self._allowed

    async def send_message(self, chat_id: str, text: str) -> None:
        await self._app.bot.send_message(chat_id=chat_id, text=text)

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

        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(CommandHandler("list", self._handle_list))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        logger.info("Telegram bot starting (polling)...")
        async with self._app:
            await self._app.start()
            await self._app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            await asyncio.get_event_loop().create_future()  # run forever

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _handle_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(str(update.effective_chat.id)):
            return
        await update.message.reply_text(
            "👋 Hi! Send me anything you want to remember.\n\n"
            "I'll remind you every 8 hours by default, or you can say:\n"
            "  • <i>Buy milk — remind me at 4pm</i>\n"
            "  • <i>Call dentist — remind me in 2 hours</i>\n"
            "  • <i>Review PR — remind me tomorrow 9am</i>\n"
            "  • <i>remind me to get off plane in 5 mins</i>",
            parse_mode=_HTML,
        )

    async def _handle_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        if not self._is_allowed(chat_id):
            return

        items = self._get_pending_items(chat_id) if self._get_pending_items else []
        if not items:
            await update.message.reply_text("📭 You have no pending reminders.")
            return

        await update.message.reply_text(
            f"📋 <b>{len(items)} pending reminder(s):</b>", parse_mode=_HTML)

        for it in items:
            if it["next_run"]:
                nr = it["next_run"]
                if nr.tzinfo is None:
                    nr = pytz.utc.localize(nr)
                next_str = nr.astimezone(self._tz).strftime("%Y-%m-%d %H:%M")
            else:
                next_str = "—"
            recur = f"every {it['repeat_hours']}h" if it["repeat_hours"] else "once"
            text = (f"📝 {_esc(it['content'])}\n"
                    f"<i>{_esc(recur)} · next: {next_str}</i>")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done", callback_data=f"done:{it['reminder_id']}"),
                InlineKeyboardButton("🗑️ Delete", callback_data=f"delete:{it['reminder_id']}"),
            ]])
            await update.message.reply_text(text, parse_mode=_HTML, reply_markup=keyboard)

    async def _handle_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        if not self._is_allowed(chat_id):
            return
        text = update.message.text.strip()
        reply = await self._on_message(chat_id, text)
        if reply:
            await update.message.reply_text(reply, parse_mode=_HTML)

    async def _handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        chat_id = str(update.effective_chat.id)
        if not self._is_allowed(chat_id):
            await query.answer()
            return
        await query.answer()

        reply = await self._on_message(chat_id, f"/callback {query.data}")
        if reply:
            await query.edit_message_text(reply, parse_mode=_HTML)
