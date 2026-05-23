"""Slack platform via slack_bolt + Socket Mode.

Lifecycle: AsyncSocketModeHandler.start_async() opens a WebSocket to
Slack and awaits forever. The Dispatcher runs this alongside the
Telegram polling loop under asyncio.gather.

Identity model: every incoming Slack event carries a `user.id`. We
translate to the canonical chat_id via the configured user_map BEFORE
entering on_message; unmapped users are silently dropped (parallels
Telegram's allowed_chat_ids rejection). The core router stays
identity-agnostic.

ForceReply ≠ Slack modal. On Telegram, the opp_update flow uses
ForceReply: prompt the user, wait for their reply, parse the original
prompt text to recover the opp id. On Slack we use a Block Kit modal
with the opp id stored in private_metadata. When the modal submits,
we synthesise `/opp update <id> <note>` and feed it back through
on_message — the existing parser does the rest. No
_handle_opp_force_reply call path on Slack.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from platforms._slack_format import actions, button, html_to_mrkdwn, section
from platforms.base import BasePlatform

logger = logging.getLogger(__name__)

# Action IDs are the same callback_data strings Telegram uses. The router
# in main.py already understands "done:N", "snooze:H:N", "opp_advance:N",
# etc. — keeping them identical means _cmd_callback is platform-blind.
_REMINDER_ACTION_PREFIXES = ("done:", "delete:", "snooze:")
_OPP_ACTION_PREFIXES = ("opp_advance:", "opp_delete:", "opp_update:")

_FORCEREPLY_OPP_RE = re.compile(r"opp\s+#(\d+)")

# Slash commands we register. Must match docs/slack-manifest.yaml.
SLASH_COMMANDS = (
    "/list", "/opp", "/invite", "/members",
    "/revoke", "/leave", "/whoami", "/help",
)


class SlackPlatform(BasePlatform):
    name = "slack"

    def __init__(self, cfg, on_message: Callable, send_reminder_fn: Callable,
                 get_pending_items: Callable = None, timezone: str = "UTC",
                 version: str = ""):
        self._bot_token = cfg.bot_token
        self._app_token = cfg.app_token
        # slack_user_id (e.g. "U01ABCDEF") → canonical chat_id ("12345").
        # Validated as numeric strings at config-load time.
        self._user_map: dict[str, str] = dict(cfg.user_map)
        # Reverse map for fan-out lookups. Last-writer-wins on collision.
        self._reverse_map: dict[str, str] = {v: k for k, v in self._user_map.items()}
        self._on_message = on_message
        self._version = version
        self._app = None  # slack_bolt.async_app.AsyncApp, set in run()

    # ── BasePlatform contract ───────────────────────────────────────────

    def identity_for(self, canonical_chat_id: str) -> Optional[str]:
        if canonical_chat_id is None:
            return None
        return self._reverse_map.get(str(canonical_chat_id))

    async def send_html(self, canonical_chat_id: str, text: str) -> None:
        slack_user = self.identity_for(canonical_chat_id)
        if slack_user is None or self._app is None:
            return
        mrk = html_to_mrkdwn(text)
        try:
            await self._app.client.chat_postMessage(
                channel=slack_user,
                text=_plain_fallback(text),
                blocks=[section(mrk)],
            )
        except Exception as e:
            logger.warning("slack send_html to %s failed: %s", slack_user, e)

    async def send_reminder(
        self, canonical_chat_id: str, content: str, reminder_id: int
    ) -> None:
        slack_user = self.identity_for(canonical_chat_id)
        if slack_user is None or self._app is None:
            return
        body = f":alarm_clock: *Reminder:* {html_to_mrkdwn(content)}"
        blocks = [
            section(body),
            actions([
                button("✅ Done", f"done:{reminder_id}", style="primary"),
                button("⏰ Snooze 2h", f"snooze:2:{reminder_id}"),
                button("⏰ Snooze 8h", f"snooze:8:{reminder_id}"),
            ]),
        ]
        try:
            await self._app.client.chat_postMessage(
                channel=slack_user,
                text=f"Reminder: {content}",
                blocks=blocks,
            )
        except Exception as e:
            logger.warning("slack send_reminder to %s failed: %s", slack_user, e)

    async def run(self) -> None:
        # Imported lazily so test environments that don't install slack_bolt
        # can still import platforms.slack for unit-testing pure helpers.
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler

        self._app = AsyncApp(token=self._bot_token)
        # Bolt's default async client lacks rate-limit retry — wire it in
        # so a burst of fan-out reminders survives 429s instead of dropping.
        self._app.client.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=2))

        self._register_handlers()

        handler = AsyncSocketModeHandler(self._app, self._app_token)
        logger.info("Slack bot starting (socket mode)...")
        await handler.start_async()

    # ── Handler registration ────────────────────────────────────────────

    def _register_handlers(self) -> None:
        app = self._app

        for cmd in SLASH_COMMANDS:
            app.command(cmd)(self._make_command_handler(cmd))

        @app.event("message")
        async def _on_message_event(event, client):
            # Ignore non-DM messages and bot echoes.
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id") or event.get("subtype"):
                return
            await self._handle_dm(event, client)

        @app.action(re.compile(r".+"))
        async def _on_action(ack, body, client, respond):
            await ack()
            await self._handle_action(body, client, respond)

        @app.view("opp_update_submit")
        async def _on_view(ack, body, client, view):
            await ack()
            await self._handle_view_submission(body, view, client)

    # ── Incoming-event handlers ─────────────────────────────────────────

    def _make_command_handler(self, cmd: str):
        async def handler(ack, body, client, respond):
            await ack()
            canonical = self._canonical_or_none(body.get("user_id"))
            if canonical is None:
                return
            text_body = (body.get("text") or "").strip()
            # parse_opp_command tolerates both "/opp ..." and "...". The
            # router's other branches key off the leading "/<cmd>" string,
            # so we reconstruct it.
            full = f"{cmd} {text_body}".rstrip()
            reply = await self._on_message(canonical, full, None, None)
            await self._render(reply, channel=body["user_id"],
                               trigger_id=body.get("trigger_id"),
                               respond=respond)
        return handler

    async def _handle_dm(self, event: dict, client) -> None:
        canonical = self._canonical_or_none(event.get("user"))
        if canonical is None:
            return
        text = (event.get("text") or "").strip()
        if not text:
            return
        reply = await self._on_message(canonical, text, None, None)
        await self._render(reply, channel=event["channel"])

    async def _handle_action(self, body: dict, client, respond) -> None:
        canonical = self._canonical_or_none(body.get("user", {}).get("id"))
        if canonical is None:
            return
        action_id = body["actions"][0]["action_id"]

        reply = await self._on_message(canonical, f"/callback {action_id}", None, None)

        # opp_update returns a force_reply prompt — on Slack we open a
        # modal with the opp id stashed in private_metadata.
        if isinstance(reply, dict) and reply.get("kind") == "force_reply":
            opp_id = _extract_opp_id(reply.get("text", "")) or _extract_action_arg(action_id)
            if opp_id is None:
                return
            await self._open_opp_update_modal(client, body["trigger_id"], opp_id)
            return

        # For plain string replies, replace the originating message in-place
        # (the Slack analogue of edit_message_text on a callback query).
        if isinstance(reply, str):
            if not reply:
                return
            await respond(replace_original=True, text=_plain_fallback(reply),
                          blocks=[section(html_to_mrkdwn(reply))])
            return

        await self._render(reply, channel=body["user"]["id"],
                           trigger_id=body.get("trigger_id"), respond=respond)

    async def _handle_view_submission(self, body: dict, view: dict, client) -> None:
        canonical = self._canonical_or_none(body.get("user", {}).get("id"))
        if canonical is None:
            return
        try:
            opp_id = int(view.get("private_metadata") or "0")
        except ValueError:
            return
        if opp_id <= 0:
            return
        note = (
            view.get("state", {})
            .get("values", {})
            .get("opp_update_block", {})
            .get("opp_update_input", {})
            .get("value")
            or ""
        ).strip()
        if not note:
            return
        # Re-enter the normal pipeline so creator-tracking, validation,
        # and reply formatting all happen the same way as on Telegram.
        synthetic = f"/opp update {opp_id} {note}"
        reply = await self._on_message(canonical, synthetic, None, None)
        await self._render(reply, channel=body["user"]["id"])

    # ── Rendering ───────────────────────────────────────────────────────

    async def _render(
        self,
        reply,
        *,
        channel: str,
        trigger_id: str | None = None,
        respond=None,
    ) -> None:
        if reply is None or reply == "":
            return
        if isinstance(reply, str):
            await self._post(channel, _plain_fallback(reply),
                             [section(html_to_mrkdwn(reply))])
            return
        if not isinstance(reply, dict):
            return

        kind = reply.get("kind")

        if kind == "reminder_cards":
            await self._render_reminder_cards(channel, reply)
            return

        if kind == "opp_cards":
            await self._render_opp_cards(channel, reply)
            return

        if kind == "csv":
            await self._render_csv(channel, reply)
            return

        if kind == "force_reply":
            # A force_reply outside the action context (e.g. raw command).
            # No trigger_id → fall back to a plain prompt.
            opp_id = _extract_opp_id(reply.get("text", ""))
            if trigger_id and opp_id is not None:
                await self._open_opp_update_modal(self._app.client, trigger_id, opp_id)
            else:
                await self._post(channel, _plain_fallback(reply["text"]),
                                 [section(html_to_mrkdwn(reply["text"]))])
            return

        logger.warning("slack: unhandled reply kind %r", kind)

    async def _render_reminder_cards(self, channel: str, reply: dict) -> None:
        if reply.get("header"):
            await self._post(channel, _plain_fallback(reply["header"]),
                             [section(html_to_mrkdwn(reply["header"]))])
        for item in reply["items"]:
            blocks = [
                section(html_to_mrkdwn(item["text"])),
                actions([
                    button("✅ Done", f"done:{item['reminder_id']}", style="primary"),
                    button("🗑️ Delete", f"delete:{item['reminder_id']}", style="danger"),
                ]),
            ]
            await self._post(channel, _plain_fallback(item["text"]), blocks)

    async def _render_opp_cards(self, channel: str, reply: dict) -> None:
        if reply.get("header"):
            await self._post(channel, _plain_fallback(reply["header"]),
                             [section(html_to_mrkdwn(reply["header"]))])
        for item in reply["items"]:
            btns = [button("📝 Update", f"opp_update:{item['opp_id']}")]
            if item.get("can_advance"):
                btns.append(button("▶ Advance", f"opp_advance:{item['opp_id']}", style="primary"))
            btns.append(button("🗑️ Delete", f"opp_delete:{item['opp_id']}", style="danger"))
            blocks = [section(html_to_mrkdwn(item["text"])), actions(btns)]
            await self._post(channel, _plain_fallback(item["text"]), blocks)

    async def _render_csv(self, channel: str, reply: dict) -> None:
        try:
            await self._app.client.files_upload_v2(
                channel=channel,
                content=reply["data"],
                filename=reply["filename"],
                initial_comment=_plain_fallback(reply.get("caption", "")) or None,
            )
        except Exception as e:
            logger.warning("slack files_upload_v2 to %s failed: %s", channel, e)

    async def _post(self, channel: str, text: str, blocks: list[dict]) -> None:
        try:
            await self._app.client.chat_postMessage(
                channel=channel, text=text or " ", blocks=blocks,
            )
        except Exception as e:
            logger.warning("slack chat_postMessage to %s failed: %s", channel, e)

    async def _open_opp_update_modal(self, client, trigger_id: str, opp_id: int) -> None:
        view = {
            "type": "modal",
            "callback_id": "opp_update_submit",
            "private_metadata": str(opp_id),
            "title": {"type": "plain_text", "text": f"Update opp #{opp_id}"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "opp_update_block",
                    "label": {"type": "plain_text", "text": "Note"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "opp_update_input",
                        "multiline": True,
                    },
                }
            ],
        }
        try:
            await client.views_open(trigger_id=trigger_id, view=view)
        except Exception as e:
            logger.warning("slack views_open failed: %s", e)

    # ── Identity translation ────────────────────────────────────────────

    def _canonical_or_none(self, slack_user_id: str | None) -> str | None:
        if not slack_user_id:
            return None
        return self._user_map.get(slack_user_id)


# ── Pure helpers (tested directly) ──────────────────────────────────────


def _plain_fallback(html: str) -> str:
    """Strip HTML tags for the Slack `text` fallback (used by notifications
    and accessibility readers). The Block Kit blocks carry the real content."""
    if not html:
        return ""
    return re.sub(r"<[^>]+>", "", html).strip()


def _extract_opp_id(text: str) -> int | None:
    m = _FORCEREPLY_OPP_RE.search(text or "")
    return int(m.group(1)) if m else None


def _extract_action_arg(action_id: str) -> int | None:
    """opp_update:42 → 42"""
    parts = (action_id or "").split(":")
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None
