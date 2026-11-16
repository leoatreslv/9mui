"""End-to-end tests of main.on_message against a real SQLite file and a
real APScheduler job store (same file, as in production)."""

import pytest

import database
import scheduler
from database import Reminder, Secretary, Thought

OWNER = "111"
SEC = "222"
STRANGER = "999"

CONFIG = f"""
platforms: [telegram]
telegram:
  token: "x"
  bot_username: "test_bot"
  allowed_chat_ids: [{OWNER}]
reminder:
  default_interval_hours: 8
timezone: "UTC"
"""


@pytest.fixture
async def app(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))

    import main
    from config import load_config
    monkeypatch.setattr(main, "cfg", load_config(str(cfg_path)))

    db_path = str(tmp_path / "reminders.db")
    database.init_db(db_path)

    async def no_send(*_a):
        pass

    scheduler.init_scheduler(db_path, no_send)
    try:
        yield main
    finally:
        scheduler._scheduler.shutdown(wait=False)
        database._engine.dispose()


def _thoughts():
    with database.get_session() as s:
        return [(t.content, t.status) for t in s.query(Thought).order_by(Thought.id)]


def _only_reminder_id():
    with database.get_session() as s:
        return s.query(Reminder).one().id


def _add_secretary():
    with database.get_session() as s:
        s.add(Secretary(owner_chat_id=OWNER, secretary_chat_id=SEC, display_name="Alice"))
        s.commit()


# ── Capture ──────────────────────────────────────────────────────────────


async def test_free_text_creates_recurring_reminder(app):
    reply = await app.on_message(OWNER, "Buy milk")
    assert "every <b>8 hours</b>" in reply
    assert _thoughts() == [("Buy milk", "pending")]
    assert scheduler.get_next_run_time(_only_reminder_id()) is not None


async def test_timed_text_creates_one_shot(app):
    reply = await app.on_message(OWNER, "Call bob tomorrow 9am")
    assert "Reminder at" in reply
    with database.get_session() as s:
        r = s.query(Reminder).one()
        assert r.repeat_hours == 0 and r.remind_at is not None


async def test_stranger_is_ignored(app):
    assert await app.on_message(STRANGER, "Buy milk") == ""
    assert _thoughts() == []


# ── Commands never become reminders ──────────────────────────────────────


@pytest.mark.parametrize("text", ["/help", "/start", "", "/list extra", "/LIST@test_bot", "/nope", "/opp list"])
async def test_commands_do_not_create_reminders(app, text):
    await app.on_message(OWNER, text)
    assert _thoughts() == []


async def test_help_and_start_show_help(app):
    for cmd in ("/help", "/start"):
        assert "Personal Reminder Bot" in await app.on_message(OWNER, cmd)


async def test_unknown_command_is_rejected(app):
    assert "Unknown command" in await app.on_message(OWNER, "/opp list")


async def test_list_shows_cards(app):
    await app.on_message(OWNER, "Buy milk")
    reply = await app.on_message(OWNER, "/list")
    assert reply["kind"] == "reminder_cards"
    assert "Buy milk" in reply["items"][0]["text"]
    assert "every 8h" in reply["items"][0]["text"]


# ── Buttons ──────────────────────────────────────────────────────────────


async def test_done_closes_reminder_and_removes_job(app):
    await app.on_message(OWNER, "Buy milk")
    rid = _only_reminder_id()
    assert "done" in await app.on_message(OWNER, f"/callback done:{rid}")
    assert _thoughts() == [("Buy milk", "done")]
    assert scheduler.get_next_run_time(rid) is None
    assert await app.on_message(OWNER, "/list") == "📭 No pending reminders."


async def test_delete_removes_thought(app):
    await app.on_message(OWNER, "Buy milk")
    rid = _only_reminder_id()
    await app.on_message(OWNER, f"/callback delete:{rid}")
    assert _thoughts() == []
    assert scheduler.get_next_run_time(rid) is None
    assert "not found" in await app.on_message(OWNER, f"/callback done:{rid}")


async def test_snooze_keeps_recurring_reminder_recurring(app):
    await app.on_message(OWNER, "Buy milk")
    rid = _only_reminder_id()
    before = scheduler.get_next_run_time(rid)

    assert "Snoozed for 2h" in await app.on_message(OWNER, f"/callback snooze:2:{rid}")

    with database.get_session() as s:
        r = s.query(Reminder).one()          # no extra row was created
        assert r.id == rid and r.is_active and r.repeat_hours == 8
    after = scheduler.get_next_run_time(rid)
    assert 5 * 3600 < (before - after).total_seconds() < 7 * 3600   # 8h → 2h away
    job = scheduler._scheduler.get_job(f"reminder_{rid}")
    assert job.trigger.interval.total_seconds() == 8 * 3600


async def test_snooze_revives_fired_one_shot(app):
    await app.on_message(OWNER, "Call bob in 5 mins")
    rid = _only_reminder_id()
    await scheduler._fire_reminder(rid, OWNER)       # fires → goes inactive
    with database.get_session() as s:
        assert s.get(Reminder, rid).is_active is False

    await app.on_message(OWNER, f"/callback snooze:8:{rid}")
    with database.get_session() as s:
        assert s.get(Reminder, rid).is_active is True
    assert scheduler.get_next_run_time(rid) is not None
    assert (await app.on_message(OWNER, "/list"))["kind"] == "reminder_cards"


async def test_snooze_after_done_is_rejected(app):
    await app.on_message(OWNER, "Buy milk")
    rid = _only_reminder_id()
    await app.on_message(OWNER, f"/callback done:{rid}")
    assert "already closed" in await app.on_message(OWNER, f"/callback snooze:2:{rid}")
    assert scheduler.get_next_run_time(rid) is None


@pytest.mark.parametrize("data", ["", "done", "done:x", "snooze:2", "snooze:0:1", "snooze:-5:1", "opp_delete:1"])
async def test_malformed_callback_is_rejected(app, data):
    assert "Unknown action" in await app.on_message(OWNER, f"/callback {data}")


# ── Secretaries ──────────────────────────────────────────────────────────


async def test_secretary_reminder_lands_on_owner_and_is_scoped(app):
    _add_secretary()
    await app.on_message(OWNER, "Owner thing")
    reply = await app.on_message(SEC, "Sec thing", "Alice")
    assert "Added to" in reply

    sec_list = await app.on_message(SEC, "/list")
    assert [i["text"].split("\n")[0] for i in sec_list["items"]] == ["📝 Sec thing"]
    owner_list = await app.on_message(OWNER, "/list")
    assert len(owner_list["items"]) == 2
    assert any("by Alice" in i["text"] for i in owner_list["items"])

    with database.get_session() as s:
        owner_rid = s.query(Reminder).join(Thought).filter(Thought.content == "Owner thing").one().id
    assert "only act on reminders you created" in await app.on_message(SEC, f"/callback done:{owner_rid}")


async def test_revoke_cancels_but_keeps_secretary_reminders(app):
    _add_secretary()
    await app.on_message(SEC, "Sec thing", "Alice")
    rid = _only_reminder_id()

    reply = await app.on_message(OWNER, f"/revoke {SEC}")
    assert "Cancelled 1 pending reminder" in reply
    assert _thoughts() == [("Sec thing", "cancelled")]     # kept, not deleted
    assert scheduler.get_next_run_time(rid) is None
    assert await app.on_message(SEC, "/list") == ""        # access gone


async def test_invite_flow(app):
    link = await app.on_message(OWNER, "/invite")
    token = link.split("?start=")[1].split('"')[0]
    assert "Welcome" in await app.on_message(SEC, f"/start {token}", "Alice")
    assert "Alice" in await app.on_message(OWNER, "/whoami")
    assert "stepped down" in await app.on_message(SEC, "/leave")
