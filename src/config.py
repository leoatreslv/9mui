import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TelegramConfig:
    token: str
    allowed_chat_ids: list[int] = field(default_factory=list)
    # Used to build invite deep-links: https://t.me/<bot_username>?start=...
    bot_username: str = ""
    # Master switch for the /invite command.
    allow_invites: bool = True
    # Per-owner cap on active (non-revoked) secretaries.
    max_secretaries_per_owner: int = 5


@dataclass
class SlackConfig:
    bot_token: str         # xoxb-...
    app_token: str         # xapp-... (for Socket Mode)
    # Map Slack user IDs (e.g. "U01ABCDEF") → canonical chat_id.
    # The canonical chat_id MUST also exist in telegram.allowed_chat_ids
    # (for an owner) or be the secretary_chat_id of an active Secretary row
    # (for a secretary). v1 piggybacks on the Telegram identity space — all
    # canonical IDs are numeric strings.
    user_map: dict[str, str] = field(default_factory=dict)


@dataclass
class DiscordConfig:
    token: str
    guild_id: Optional[str] = None


@dataclass
class WhatsAppConfig:
    token: str
    phone_number_id: str
    verify_token: str


@dataclass
class ReminderConfig:
    default_interval_hours: int = 8


@dataclass
class AppConfig:
    # Legacy single-platform field, retained for backward compatibility.
    # New deployments should use `platforms` instead.
    platform: str
    # Ordered list of enabled platforms. Wins over `platform` when set.
    platforms: list[str]
    timezone: str
    reminder: ReminderConfig
    telegram: Optional[TelegramConfig] = None
    slack: Optional[SlackConfig] = None
    discord: Optional[DiscordConfig] = None
    whatsapp: Optional[WhatsAppConfig] = None


def load_config(path: str = None) -> AppConfig:
    if path is None:
        path = os.environ.get("CONFIG_PATH", "/app/config.yaml")

    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    reminder = ReminderConfig(**raw.get("reminder", {}))
    timezone = raw.get("timezone", "UTC")
    platform = raw.get("platform", "telegram").lower()
    platforms = [p.lower() for p in raw.get("platforms", [])] or [platform]

    telegram = None
    if "telegram" in raw:
        tg = raw["telegram"].copy()
        tg.setdefault("allowed_chat_ids", [])
        tg.setdefault("bot_username", "")
        tg.setdefault("allow_invites", True)
        tg.setdefault("max_secretaries_per_owner", 5)
        telegram = TelegramConfig(**tg)

    slack = None
    if "slack" in raw:
        sl = raw["slack"].copy()
        sl.setdefault("user_map", {})
        # v1 invariant: every canonical chat_id is a numeric string. This
        # keeps `int(chat_id)` checks in secretary.py / main.py safe and
        # means the canonical identity space is shared with Telegram.
        user_map = sl.get("user_map") or {}
        for slack_uid, canonical in user_map.items():
            if not isinstance(canonical, (str, int)) or not str(canonical).lstrip("-").isdigit():
                raise ValueError(
                    f"slack.user_map[{slack_uid!r}] = {canonical!r} is not a numeric "
                    "chat_id. v1 requires canonical IDs to match Telegram's numeric "
                    "format."
                )
        sl["user_map"] = {k: str(v) for k, v in user_map.items()}
        slack = SlackConfig(**sl)

    discord = None
    if "discord" in raw:
        discord = DiscordConfig(**raw["discord"])

    whatsapp = None
    if "whatsapp" in raw:
        whatsapp = WhatsAppConfig(**raw["whatsapp"])

    return AppConfig(
        platform=platform,
        platforms=platforms,
        timezone=timezone,
        reminder=reminder,
        telegram=telegram,
        slack=slack,
        discord=discord,
        whatsapp=whatsapp,
    )
