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
    platform: str
    timezone: str
    reminder: ReminderConfig
    telegram: Optional[TelegramConfig] = None
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

    telegram = None
    if "telegram" in raw:
        tg = raw["telegram"].copy()
        tg.setdefault("allowed_chat_ids", [])
        tg.setdefault("bot_username", "")
        tg.setdefault("allow_invites", True)
        tg.setdefault("max_secretaries_per_owner", 5)
        telegram = TelegramConfig(**tg)

    discord = None
    if "discord" in raw:
        discord = DiscordConfig(**raw["discord"])

    whatsapp = None
    if "whatsapp" in raw:
        whatsapp = WhatsAppConfig(**raw["whatsapp"])

    return AppConfig(
        platform=platform,
        timezone=timezone,
        reminder=reminder,
        telegram=telegram,
        discord=discord,
        whatsapp=whatsapp,
    )
