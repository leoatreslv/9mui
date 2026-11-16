"""SlackPlatform pure-function and identity-translation tests.

slack_bolt is heavy and async — we don't exercise the WebSocket loop.
Tests focus on the seams: identity translation, payload → on_message
synthesis, and the dict→Block Kit rendering helpers.
"""

import asyncio
from types import SimpleNamespace

import pytest

from platforms.slack import SLASH_COMMANDS, SlackPlatform, _plain_fallback


SLACK_USER = "U01ABCDEF"
CANONICAL = "111"


def _make_cfg(user_map=None):
    return SimpleNamespace(
        bot_token="xoxb-test",
        app_token="xapp-test",
        user_map=user_map if user_map is not None else {SLACK_USER: CANONICAL},
    )


def _make_platform(on_message=None, user_map=None) -> SlackPlatform:
    on_message = on_message or (lambda *_args, **_kw: None)
    return SlackPlatform(_make_cfg(user_map), on_message)


# ── Identity translation ─────────────────────────────────────────────────


def test_identity_for_known_canonical():
    p = _make_platform()
    assert p.identity_for(CANONICAL) == SLACK_USER


def test_identity_for_unknown_returns_none():
    p = _make_platform()
    assert p.identity_for("999") is None


def test_identity_for_none_chat_id():
    p = _make_platform()
    assert p.identity_for(None) is None


def test_canonical_or_none_known():
    p = _make_platform()
    assert p._canonical_or_none(SLACK_USER) == CANONICAL


def test_canonical_or_none_unknown_drops():
    p = _make_platform()
    assert p._canonical_or_none("U99NOTMAPPED") is None


def test_canonical_or_none_empty_drops():
    p = _make_platform()
    assert p._canonical_or_none("") is None
    assert p._canonical_or_none(None) is None


# ── Pure helpers ─────────────────────────────────────────────────────────


def test_plain_fallback_strips_tags():
    assert _plain_fallback("<b>Hi</b> <i>there</i>") == "Hi there"


def test_plain_fallback_empty():
    assert _plain_fallback("") == ""


def test_no_opp_slash_command_registered():
    assert "/opp" not in SLASH_COMMANDS
    assert "/help" in SLASH_COMMANDS


# ── Slash command handler synthesis ──────────────────────────────────────


@pytest.mark.asyncio
async def test_command_handler_translates_user_and_reconstructs_command():
    """Slash commands deliver `text` as the body only. The handler must
    re-prefix with the command name so the existing on_message router
    matches its leading '/list' / '/revoke' branches."""
    captured = {}

    async def fake_on_message(chat_id, text):
        captured["args"] = (chat_id, text)
        return ""

    p = _make_platform(on_message=fake_on_message)
    handler = p._make_command_handler("/revoke")

    async def ack():
        captured["acked"] = True

    body = {"user_id": SLACK_USER, "text": "  877247421 ",
            "trigger_id": "trig1"}

    async def respond(**_kw):
        captured["responded"] = True

    await handler(ack, body, client=None, respond=respond)

    assert captured["acked"] is True
    assert captured["args"] == (CANONICAL, "/revoke 877247421")


@pytest.mark.asyncio
async def test_command_handler_drops_unmapped_user():
    captured = {"called": False}

    async def fake_on_message(*_args, **_kw):
        captured["called"] = True
        return ""

    p = _make_platform(on_message=fake_on_message)
    handler = p._make_command_handler("/list")

    async def ack():
        pass

    async def respond(**_kw):
        pass

    body = {"user_id": "U99NOTMAPPED", "text": "", "trigger_id": "x"}
    await handler(ack, body, client=None, respond=respond)

    assert captured["called"] is False


@pytest.mark.asyncio
async def test_command_handler_empty_body_passes_bare_command():
    captured = {}

    async def fake_on_message(chat_id, text, *_a):
        captured["text"] = text
        return ""

    p = _make_platform(on_message=fake_on_message)
    handler = p._make_command_handler("/list")

    async def ack():
        pass

    async def respond(**_kw):
        pass

    body = {"user_id": SLACK_USER, "text": "", "trigger_id": "x"}
    await handler(ack, body, client=None, respond=respond)
    # No trailing space — bare "/list" is what the router expects.
    assert captured["text"] == "/list"


# ── DM message handler ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dm_handler_translates_and_passes_text():
    captured = {}

    async def fake_on_message(chat_id, text, *_a):
        captured["chat_id"] = chat_id
        captured["text"] = text
        return ""

    p = _make_platform(on_message=fake_on_message)
    event = {"user": SLACK_USER, "text": "buy milk tomorrow at 4pm", "channel": "D1"}
    await p._handle_dm(event, client=None)
    assert captured["chat_id"] == CANONICAL
    assert captured["text"] == "buy milk tomorrow at 4pm"


@pytest.mark.asyncio
async def test_dm_handler_drops_unmapped():
    captured = {"called": False}

    async def fake_on_message(*_a, **_kw):
        captured["called"] = True
        return ""

    p = _make_platform(on_message=fake_on_message)
    event = {"user": "U99", "text": "x", "channel": "D1"}
    await p._handle_dm(event, client=None)
    assert captured["called"] is False
