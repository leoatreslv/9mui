from config import AppConfig
from platforms.base import BasePlatform


def load_platform(cfg: AppConfig, on_message, send_reminder_fn,
                  get_pending_items=None, version: str = "") -> BasePlatform:
    """Single-platform loader, kept for backward compatibility with the
    legacy `platform: telegram` config form. New deployments should use
    `platforms: [telegram, slack]` and load_all_platforms()."""
    return _instantiate(cfg.platform, cfg, on_message, send_reminder_fn,
                        get_pending_items, version)


def load_all_platforms(cfg: AppConfig, on_message, send_reminder_fn,
                       get_pending_items=None, version: str = "") -> list[BasePlatform]:
    """Instantiate every platform listed in cfg.platforms.

    Order matters only for log output. Each platform runs concurrently
    under the Dispatcher.
    """
    names = list(cfg.platforms) or [cfg.platform]
    return [
        _instantiate(name, cfg, on_message, send_reminder_fn,
                     get_pending_items, version)
        for name in names
    ]


def _instantiate(name: str, cfg: AppConfig, on_message, send_reminder_fn,
                 get_pending_items, version: str) -> BasePlatform:
    if name == "telegram":
        if cfg.telegram is None:
            raise ValueError("platform 'telegram' enabled but no [telegram] config block.")
        from platforms.telegram import TelegramPlatform
        return TelegramPlatform(cfg.telegram, on_message, send_reminder_fn,
                                get_pending_items, cfg.timezone, version)

    if name == "slack":
        if cfg.slack is None:
            raise ValueError("platform 'slack' enabled but no [slack] config block.")
        from platforms.slack import SlackPlatform
        return SlackPlatform(cfg.slack, on_message, send_reminder_fn,
                             get_pending_items, cfg.timezone, version)

    raise ValueError(
        f"Unsupported platform: {name!r}. Supported: telegram, slack."
    )
