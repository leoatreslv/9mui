"""SlackPlatform pure-function and identity-translation tests.

slack_bolt is heavy and async — we don't exercise the WebSocket loop.
Tests focus on the seams: identity translation, payload → on_message
synthesis, and the dict→Block Kit rendering helpers.
"""

import asyncio
from types import SimpleNamespace

import pytest

from platforms.slack import (
    SlackPlatform,
    _extract_action_arg,
    _extract_opp_id,
    _plain_fallback,
)


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
    return SlackPlatform(
        _make_cfg(user_map), on_message, lambda *_a, **_k: None,
        get_pending_items=None, timezone="UTC", version="test",
    )


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


def test_extract_opp_id_from_force_reply():
    text = "📝 Reply to this message with the update note for opp #42 (Acme deal):"
    assert _extract_opp_id(text) == 42


def test_extract_opp_id_none():
    assert _extract_opp_id("no opp tag here") is None


def test_extract_action_arg():
    assert _extract_action_arg("opp_update:42") == 42
    assert _extract_action_arg("done:7") == 7


def test_extract_action_arg_missing():
    assert _extract_action_arg("invalid") is None
    assert _extract_action_arg("") is None


# ── Slash command handler synthesis ──────────────────────────────────────


@pytest.mark.asyncio
async def test_command_handler_translates_user_and_reconstructs_command():
    """Slash commands deliver `text` as the body only. The handler must
    re-prefix with the command name so the existing on_message router
    matches its leading '/list' / '/opp' branches."""
    captured = {}

    async def fake_on_message(chat_id, text, reply_to, first_name):
        captured["args"] = (chat_id, text, reply_to, first_name)
        return ""

    p = _make_platform(on_message=fake_on_message)
    handler = p._make_command_handler("/opp")

    async def ack():
        captured["acked"] = True

    body = {"user_id": SLACK_USER, "text": "new Acme --customer=Acme Corp",
            "trigger_id": "trig1"}

    async def respond(**_kw):
        captured["responded"] = True

    await handler(ack, body, client=None, respond=respond)

    assert captured["acked"] is True
    chat_id, text, reply_to, first_name = captured["args"]
    assert chat_id == CANONICAL
    assert text == "/opp new Acme --customer=Acme Corp"
    assert reply_to is None
    assert first_name is None


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


# ── Modal view submission ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_submission_synthesises_opp_update_command():
    """When a Slack modal carries the opp id in private_metadata and a
    note in the input block, we re-enter on_message with the standard
    /opp update text so the normal parser, actor tracking, and reply
    rendering all kick in."""
    captured = {}

    async def fake_on_message(chat_id, text, *_a):
        captured["chat_id"] = chat_id
        captured["text"] = text
        return ""

    p = _make_platform(on_message=fake_on_message)

    body = {"user": {"id": SLACK_USER}}
    view = {
        "private_metadata": "42",
        "state": {
            "values": {
                "opp_update_block": {
                    "opp_update_input": {"value": "  spoke with finance  "}
                }
            }
        },
    }
    await p._handle_view_submission(body, view, client=None)

    assert captured["chat_id"] == CANONICAL
    assert captured["text"] == "/opp update 42 spoke with finance"


@pytest.mark.asyncio
async def test_view_submission_drops_empty_note():
    captured = {"called": False}

    async def fake_on_message(*_a, **_kw):
        captured["called"] = True
        return ""

    p = _make_platform(on_message=fake_on_message)
    body = {"user": {"id": SLACK_USER}}
    view = {
        "private_metadata": "42",
        "state": {"values": {"opp_update_block": {"opp_update_input": {"value": "  "}}}},
    }
    await p._handle_view_submission(body, view, client=None)
    assert captured["called"] is False


@pytest.mark.asyncio
async def test_view_submission_drops_unmapped_user():
    captured = {"called": False}

    async def fake_on_message(*_a, **_kw):
        captured["called"] = True
        return ""

    p = _make_platform(on_message=fake_on_message)
    body = {"user": {"id": "U99NOTMAPPED"}}
    view = {
        "private_metadata": "1",
        "state": {"values": {"opp_update_block": {"opp_update_input": {"value": "x"}}}},
    }
    await p._handle_view_submission(body, view, client=None)
    assert captured["called"] is False


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
