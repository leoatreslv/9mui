from abc import ABC, abstractmethod
from typing import Optional


class BasePlatform(ABC):
    """Abstract base for chat platforms.

    Each platform owns its own transport (polling, sockets, webhooks)
    and translates between its native message shapes and the
    platform-neutral structured responses produced by on_message.

    Identity model: callers refer to users by canonical chat_id (a
    numeric string in v1, sharing Telegram's identity space). Each
    platform implements identity_for() to declare whether it can reach
    a given canonical id on its transport.
    """

    # Used in logs and for routing. Concrete platforms set this.
    name: str = "base"

    @abstractmethod
    def identity_for(self, canonical_chat_id: str) -> Optional[str]:
        """Return the platform-specific routing handle for a canonical id,
        or None if this platform has no way to reach that user.

        For Telegram: returns the chat_id itself (since canonical == Telegram).
        For Slack: returns the Slack user ID from user_map, else None.
        """

    @abstractmethod
    async def send_html(self, canonical_chat_id: str, text: str) -> None:
        """Send an out-of-band HTML message (e.g. owner notifications).

        Implementations should silently no-op when identity_for() is None
        so dispatch fan-out is cheap on platforms a user isn't on.
        """

    @abstractmethod
    async def send_reminder(
        self, canonical_chat_id: str, content: str, reminder_id: int
    ) -> None:
        """Deliver a fired reminder, with Done/Snooze action buttons."""

    @abstractmethod
    async def run(self) -> None:
        """Start the platform listener. Awaits forever (or until cancelled)."""
