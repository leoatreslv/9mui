from config import AppConfig
from platforms.base import BasePlatform


def load_platform(cfg: AppConfig, on_message, send_reminder_fn,
                  get_pending_items=None) -> BasePlatform:
    """Factory: returns the configured platform instance."""
    if cfg.platform == "telegram":
        from platforms.telegram import TelegramPlatform
        return TelegramPlatform(cfg.telegram, on_message, send_reminder_fn, get_pending_items, cfg.timezone)

    # Future platforms:
    # if cfg.platform == "discord":
    #     from platforms.discord import DiscordPlatform
    #     return DiscordPlatform(cfg.discord, on_message, send_reminder_fn)
    #
    # if cfg.platform == "whatsapp":
    #     from platforms.whatsapp import WhatsAppPlatform
    #     return WhatsAppPlatform(cfg.whatsapp, on_message, send_reminder_fn)

    raise ValueError(f"Unsupported platform: '{cfg.platform}'. "
                     f"Supported: telegram (discord/whatsapp coming soon)")
