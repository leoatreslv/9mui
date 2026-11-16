"""TelegramPlatform handler seams, with python-telegram-bot objects faked."""

from types import SimpleNamespace

from platforms.telegram import TelegramPlatform


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **_kw):
        self.replies.append(text)


def _update(text, chat_id=111, first_name="Leo"):
    return SimpleNamespace(
        message=FakeMessage(text),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(first_name=first_name),
    )


def _platform(reply):
    calls = []

    async def on_message(chat_id, text, first_name=None):
        calls.append((chat_id, text, first_name))
        return reply

    return TelegramPlatform(SimpleNamespace(token="x"), on_message), calls


async def test_text_is_routed_with_string_chat_id_and_name():
    p, calls = _platform("ok")
    upd = _update("  Buy milk ")
    await p._handle_text(upd, None)
    assert calls == [("111", "Buy milk", "Leo")]
    assert upd.message.replies == ["ok"]


async def test_start_with_invite_token_reaches_router_verbatim():
    p, calls = _platform("welcome")
    await p._handle_text(_update("/start invite_abc"), None)
    assert calls[0][1] == "/start invite_abc"


async def test_bare_start_from_unknown_chat_gets_a_greeting():
    p, _ = _platform("")
    upd = _update("/start")
    await p._handle_text(upd, None)
    assert len(upd.message.replies) == 1 and "invite link" in upd.message.replies[0]


async def test_unknown_chat_free_text_is_ignored():
    p, _ = _platform("")
    upd = _update("hello")
    await p._handle_text(upd, None)
    assert upd.message.replies == []


def test_identity_for():
    p, _ = _platform("")
    assert p.identity_for("123") == "123"
    assert p.identity_for("U01ABC") is None
    assert p.identity_for(None) is None
