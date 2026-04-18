from abc import ABC, abstractmethod


class BasePlatform(ABC):
    """
    Abstract base for chat platforms.
    Implement this to add Discord, WhatsApp, etc.
    """

    @abstractmethod
    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a plain text message to a user/chat."""

    @abstractmethod
    async def run(self) -> None:
        """Start the platform listener (polling or webhook). Blocks until stopped."""
