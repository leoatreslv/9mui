"""Owns the list of active platforms and fans cross-platform sends out.

The dispatcher exists so that send_reminder (called by APScheduler) and
owner notifications (_notify in main.py) don't need to know which
platforms are live — they just hand the canonical chat_id and the
dispatcher delivers to every platform that can reach the user.

A single fan-out failure on one platform does not abort the others;
errors are logged and the next platform is attempted.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from platforms.base import BasePlatform

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, platforms: Iterable[BasePlatform]):
        self._platforms: list[BasePlatform] = list(platforms)
        if not self._platforms:
            raise ValueError("Dispatcher requires at least one platform.")

    @property
    def platforms(self) -> list[BasePlatform]:
        return list(self._platforms)

    async def send_reminder(self, chat_id: str, content: str, reminder_id: int) -> None:
        """Fire a reminder to every platform that can reach chat_id.

        Stale-button caveat: each platform's card has independent buttons.
        Clicking Done on one does not edit the duplicate on the other;
        the second click is rejected idempotently by the callback handler.
        """
        for p in self._platforms:
            if p.identity_for(chat_id) is None:
                continue
            try:
                await p.send_reminder(chat_id, content, reminder_id)
            except Exception:
                logger.exception("send_reminder failed on platform %s", p.name)

    async def send_html(self, chat_id: str, text: str) -> None:
        """Best-effort cross-platform notification (e.g. invite-accepted)."""
        for p in self._platforms:
            if p.identity_for(chat_id) is None:
                continue
            try:
                await p.send_html(chat_id, text)
            except Exception:
                logger.exception("send_html failed on platform %s", p.name)

    async def run(self) -> None:
        """Run every platform concurrently. A crash on one is logged but
        doesn't tear down the others — gather with return_exceptions
        keeps the surviving platforms alive."""
        results = await asyncio.gather(
            *(p.run() for p in self._platforms),
            return_exceptions=True,
        )
        for p, result in zip(self._platforms, results):
            if isinstance(result, Exception):
                logger.error("Platform %s exited with error: %s", p.name, result)
