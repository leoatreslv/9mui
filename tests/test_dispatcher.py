"""Dispatcher fan-out across multiple platforms."""

import asyncio
from typing import Optional

import pytest

from dispatcher import Dispatcher
from platforms.base import BasePlatform


class FakePlatform(BasePlatform):
    def __init__(self, name: str, reachable_ids: set[str], *, fail_on=None):
        self.name = name
        self._reachable = set(reachable_ids)
        self._fail_on = fail_on  # method name to raise on
        self.send_reminder_calls: list[tuple] = []
        self.send_html_calls: list[tuple] = []

    def identity_for(self, canonical_chat_id: str) -> Optional[str]:
        return f"{self.name}:{canonical_chat_id}" if canonical_chat_id in self._reachable else None

    async def send_html(self, canonical_chat_id: str, text: str) -> None:
        if self._fail_on == "send_html":
            raise RuntimeError("boom")
        self.send_html_calls.append((canonical_chat_id, text))

    async def send_reminder(self, canonical_chat_id: str, content: str, reminder_id: int) -> None:
        if self._fail_on == "send_reminder":
            raise RuntimeError("boom")
        self.send_reminder_calls.append((canonical_chat_id, content, reminder_id))

    async def run(self) -> None:
        await asyncio.sleep(0)


def test_dispatcher_requires_at_least_one_platform():
    with pytest.raises(ValueError):
        Dispatcher([])


@pytest.mark.asyncio
async def test_send_reminder_fans_out_to_reachable_platforms():
    tg = FakePlatform("tg", reachable_ids={"111", "222"})
    sl = FakePlatform("sl", reachable_ids={"111"})
    disp = Dispatcher([tg, sl])

    await disp.send_reminder("111", "hi", 1)

    assert tg.send_reminder_calls == [("111", "hi", 1)]
    assert sl.send_reminder_calls == [("111", "hi", 1)]


@pytest.mark.asyncio
async def test_send_reminder_skips_unreachable_platform():
    tg = FakePlatform("tg", reachable_ids={"111"})
    sl = FakePlatform("sl", reachable_ids=set())  # user not on Slack
    disp = Dispatcher([tg, sl])

    await disp.send_reminder("111", "hi", 1)

    assert tg.send_reminder_calls == [("111", "hi", 1)]
    assert sl.send_reminder_calls == []


@pytest.mark.asyncio
async def test_send_reminder_one_failure_does_not_block_other():
    tg = FakePlatform("tg", reachable_ids={"111"}, fail_on="send_reminder")
    sl = FakePlatform("sl", reachable_ids={"111"})
    disp = Dispatcher([tg, sl])

    await disp.send_reminder("111", "hi", 1)

    # Telegram raised but Slack still got the call.
    assert sl.send_reminder_calls == [("111", "hi", 1)]


@pytest.mark.asyncio
async def test_send_html_fans_out_and_skips_unreachable():
    tg = FakePlatform("tg", reachable_ids={"111"})
    sl = FakePlatform("sl", reachable_ids={"222"})
    disp = Dispatcher([tg, sl])

    await disp.send_html("111", "hello")

    assert tg.send_html_calls == [("111", "hello")]
    assert sl.send_html_calls == []
