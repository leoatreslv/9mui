# 9Mui — Personal Reminder Bot

![Version](https://img.shields.io/badge/version-0.1-blue)
![Docker Pulls](https://img.shields.io/docker/pulls/wongleo/reminder-bot)

A self-hosted thought-capture and reminder bot. Send it anything you want to remember — it stores it and reminds you at the right time. Runs on Telegram, deployed via Docker.

**No AI required.** This bot runs entirely on your own machine with no external AI services, API keys, or subscriptions. Just a Telegram bot token and Docker.

## Features

- **Natural language time parsing** — English and Chinese
- **Persistent storage** — SQLite, survives reboots
- **Inline actions** — Done, Snooze, Delete without leaving the chat
- **Access control** — restrict to your own Telegram ID
- **Extensible** — platform abstraction ready for Discord, WhatsApp, LINE

## Quick Start

The Docker image is publicly available on Docker Hub — no need to build anything.

**1. Get a Telegram bot token**

Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token.

**2. Find your Telegram chat ID**

Message [@userinfobot](https://t.me/userinfobot) → it replies with your numeric ID.

**3. Configure**

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`:

```yaml
platform: telegram

telegram:
  token: "YOUR_BOT_TOKEN"
  allowed_chat_ids:
    - 123456789   # your Telegram ID

reminder:
  default_interval_hours: 8

timezone: "Asia/Singapore"   # your timezone
```

**4. Pull the image and run**

```bash
docker pull wongleo/reminder-bot
docker compose up -d
```

> The image is hosted on [Docker Hub](https://hub.docker.com/r/wongleo/reminder-bot). No build step needed.

## Usage

Just send a message to your bot:

| Message | Behaviour |
|---|---|
| `Buy groceries` | Saved, reminded every 8 hours |
| `Call dentist — remind me at 4pm` | Reminded once at 4pm |
| `remind me to get off plane in 5 mins` | Reminded in 5 minutes |
| `remind me to call dentist at 4pm` | Reminded once at 4pm |
| `Meeting notes by 3pm` | Reminded once at 3pm |
| `提醒我明天下午4點打電話給醫生` | Reminded tomorrow at 4pm |
| `提醒我5分鐘後下飛機` | Reminded in 5 minutes |
| `叫老婆買菜 明天下午3點` | Reminded tomorrow at 3pm |

**Commands:**

| Command | Action |
|---|---|
| `/list` | Show all pending reminders with Done / Delete buttons |
| `/start` | Show help |

**Reminder actions (inline buttons):**

- **✅ Done** — mark complete and cancel future reminders
- **⏰ Snooze 2h / 8h** — delay the reminder
- **🗑️ Delete** — permanently delete the reminder

## Supported Time Expressions

**English:**
- `at 4pm`, `by 3pm`, `at 10:30`
- `in 2 hours`, `in 30 minutes`, `in 5 mins`
- `tomorrow`, `tomorrow 9am`
- `next Monday`, `next week`
- `remind me to X [time]`, `remind me at [time]`

**Chinese:**
- `明天下午4點` (tomorrow 4pm)
- `今天早上9點` (today 9am)
- `下午3點` (3pm)
- `5分鐘後` (in 5 minutes)
- `1小時後` (in 1 hour)
- `提醒我...`, `幫我提醒...`, `提醒一下...`

## Configuration Reference

```yaml
platform: telegram          # telegram | discord | whatsapp (future)

telegram:
  token: "..."              # from @BotFather
  allowed_chat_ids:         # leave empty to allow all users
    - 123456789

reminder:
  default_interval_hours: 8 # repeat interval when no time is given

timezone: "Asia/Singapore"  # any tz database name
```

Full list of timezone names: [Wikipedia — List of tz database time zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)

## Adding a New Platform

1. Create `src/platforms/<name>.py` implementing `BasePlatform`:

```python
from platforms.base import BasePlatform

class MyPlatform(BasePlatform):
    async def send_message(self, chat_id: str, text: str) -> None: ...
    async def send_reminder(self, chat_id: str, content: str, reminder_id: int) -> None: ...
    async def run(self) -> None: ...
```

2. Register it in `src/platforms/__init__.py`
3. Add credentials to `config.example.yaml`
4. Set `platform: <name>` in your `config.yaml`

## Project Structure

```
├── src/
│   ├── main.py              # message routing, reminder callbacks
│   ├── config.py            # config loader
│   ├── database.py          # SQLAlchemy models (Thought, Reminder)
│   ├── scheduler.py         # APScheduler wrapper
│   ├── time_parser.py       # NLP time extraction (EN + ZH)
│   └── platforms/
│       ├── base.py          # abstract platform interface
│       └── telegram.py      # Telegram implementation
├── config.example.yaml
├── docker-compose.yml
└── Dockerfile
```

## Tech Stack

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v21
- [APScheduler](https://apscheduler.readthedocs.io/) v3 — job persistence via SQLite
- [SQLAlchemy](https://www.sqlalchemy.org/) v2 — ORM
- [dateparser](https://dateparser.readthedocs.io/) — multilingual time parsing
- Docker + Docker Compose

## License

MIT
