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
| `/list` | Show pending reminders |
| `/opp` | Sales-opportunity tracker (see section below) |
| `/invite` | Invite a secretary to help on your behalf (owner only) |
| `/members` | Show your secretaries (owner) or your owner (secretary) |
| `/revoke <chat_id>` | Revoke a secretary (owner only) |
| `/leave` | Step down as a secretary |
| `/whoami` | Show your role |
| `/help` | In-bot help |

**Reminder actions (inline buttons):**

- **✅ Done** — mark complete and cancel future reminders
- **⏰ Snooze 2h / 8h** — delay the reminder
- **🗑️ Delete** — permanently delete the reminder

## Secretaries

You can invite other Telegram users to act as your secretaries — they
help by adding reminders and managing your sales pipeline. Everything
they do lands on **your** list; reminder pings go to **you**.

### Inviting

1. Run `/invite` — the bot replies with a deep-link valid for 24 hours.
2. Send that link to the person you want to invite (DM, email, anywhere).
3. They tap the link → Telegram opens → they tap **Start**.
4. The bot confirms; you get a notification: "✅ Alice (…7421) accepted your invite."
5. They're now your secretary.

Caps: up to **5 active secretaries** per owner (configurable), up to **10
pending invite tokens** at a time. Invites are single-use.

⚠️ Anyone who opens the link within 24h becomes your secretary — don't
forward it or post it publicly.

### What a secretary can do

| Action | Secretary | Owner |
|---|---|---|
| Send a free-text reminder ("Buy milk 4pm") | ✅ lands on owner's list, pings owner | ✅ |
| `/list` | only reminders they themselves added | full list |
| Mark done / snooze / delete a reminder | only on their own additions | any |
| All `/opp` commands (new, update, stage, delete, export, list) | ✅ full assistant access | ✅ |
| `/opp list` | sees owner's full pipeline | ✅ |
| `/invite`, `/revoke` | ❌ | ✅ |
| `/leave` | ✅ steps down | n/a |
| `/whoami` | ✅ shows their owner | ✅ shows secretaries |

Reminder cards delivered to the owner are prefixed `(via Alice)` so you
always know who added what. Opportunity updates show the same
attribution in `/opp show`, `/opp list md`, and the CSV export.

### Revoking

```
/revoke 877247421
```

Revocation is a soft-delete: the secretary loses access immediately and
any pending reminders they created (that haven't fired yet) are
cancelled. Their past contributions remain in your data and continue to
show "by Alice (former)".

### Disabling invites

Set `telegram.allow_invites: false` in `config.yaml` and restart. The
`/invite` command will refuse for everyone.

## Sales Opportunities

Track deals in progress alongside your reminders. Each opportunity has
a title, customer, stage (`lead → qualified → proposal → won | lost`),
and an append-only update log.

| Command | Action |
|---|---|
| `/opp new <title> [--customer=<name>]` | Create a new opportunity |
| `/opp update <id> <note>` | Append a timestamped update |
| `/opp stage <id> <stage>` | Change stage (also logs an audit entry) |
| `/opp show <id>` | Show one opportunity with full update history |
| `/opp list [md] [stage]` | List opportunities (cards by default; `md` = markdown table; optional stage filter) |
| `/opp export csv` | Send a UTF-8-with-BOM CSV file as a Telegram attachment (Excel-friendly) |
| `/opp delete <id>` | Soft-delete (row stays in DB, filtered out of list) |
| `/opp help` | Show this list inside Telegram |

**Examples:**

```
/opp new Acme Q1 renewal --customer=Acme Corp
/opp update 1 Met with Bob, looking good
/opp stage 1 proposal
/opp list
/opp list won
/opp list md
/opp export csv
```

**Inline buttons on each card:**

- **📝 Update** — bot prompts (ForceReply); your next reply becomes the update note
- **▶ Advance** — move to the next stage (`lead → qualified → proposal → won`)
- **🗑️ Delete** — soft-delete

Stages are validated; `/opp stage 5 wun` is rejected. Stage changes
also write a real update row (`stage: proposal → won`) so the history
in `/opp show` and the CSV export is complete.

Opportunities are scoped to your Telegram chat ID — if the bot has
multiple `allowed_chat_ids`, users cannot see or modify each other's
opportunities.

## Development

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src python -m pytest tests -q
```

Tests cover the `/opp` command parser, the service layer (CRUD,
multi-user isolation, stage transitions, soft-delete), and the
formatters (cards, markdown table, CSV export shape with escaped
quotes/commas/newlines).

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
│   ├── main.py              # central router (reminders, /opp, /invite, callbacks)
│   ├── config.py            # config loader
│   ├── database.py          # SQLAlchemy models (Thought / Reminder / Opportunity
│   │                        #   / OpportunityUpdate / Secretary / Invite / SecretaryEvent)
│   ├── migrations.py        # idempotent SQLite migrations (ALTER TABLE + backup)
│   ├── secretary.py         # resolve_owner, invite lifecycle, format_actor, audit
│   ├── scheduler.py         # APScheduler wrapper
│   ├── time_parser.py       # NLP time extraction (EN + ZH)
│   ├── opp.py               # /opp parser, service, formatters (cards/md/CSV)
│   └── platforms/
│       ├── base.py          # abstract platform interface
│       └── telegram.py      # Telegram implementation
├── tests/                   # pytest suite (96 tests)
├── config.example.yaml
├── requirements.txt
├── requirements-dev.txt
├── docker-compose.yml
└── Dockerfile
```

## Database migration

The first boot after upgrading to a release with the secretary feature
will:

1. Copy your existing `reminders.db` to `reminders.db.bak-pre-secretary`
   (only if no backup exists yet)
2. `ALTER TABLE thoughts ADD COLUMN created_by_chat_id TEXT` if it's missing
3. Backfill `created_by_chat_id = chat_id` on all existing rows
4. Create the new `secretaries`, `invites`, and `secretary_events` tables

All steps are idempotent — restarting the bot doesn't repeat them. If
you ever need to roll back, swap the backup file in for the live DB.

## Tech Stack

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v21
- [APScheduler](https://apscheduler.readthedocs.io/) v3 — job persistence via SQLite
- [SQLAlchemy](https://www.sqlalchemy.org/) v2 — ORM
- [dateparser](https://dateparser.readthedocs.io/) — multilingual time parsing
- Docker + Docker Compose

## License

MIT
