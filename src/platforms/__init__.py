from config import AppConfig
from platforms.base import BasePlatform


def load_all_platforms(cfg: AppConfig, on_message) -> list[BasePlatform]:
    """Instantiate every platform listed in cfg.platforms.

    Order matters only for log output. Each platform runs concurrently
    under the Dispatcher.
    """
    names = list(cfg.platforms) or [cfg.platform]
    return [_instantiate(name, cfg, on_message) for name in names]


def _instantiate(name: str, cfg: AppConfig, on_message) -> BasePlatform:
    if name == "telegram":
        if cfg.telegram is None:
            raise ValueError("platform 'telegram' enabled but no [telegram] config block.")
        from platforms.telegram import TelegramPlatform
        return TelegramPlatform(cfg.telegram, on_message)

    if name == "slack":
        if cfg.slack is None:
            raise ValueError("platform 'slack' enabled but no [slack] config block.")
        from platforms.slack import SlackPlatform
        return SlackPlatform(cfg.slack, on_message)

    raise ValueError(
        f"Unsupported platform: {name!r}. Supported: telegram, slack."
    )
