# 9Mui — Personal Reminder Bot

![Version](https://img.shields.io/badge/version-0.2-blue)
![Docker Pulls](https://img.shields.io/docker/pulls/wongleo/reminder-bot)

A self-hosted thought-capture and reminder bot. Send it anything you want to remember — it stores it and reminds you at the right time. Runs on Telegram and/or Slack (concurrently), deployed via Docker.

**No AI required.** This bot runs entirely on your own machine with no external AI services, API keys, or subscriptions. Just a Telegram bot token and Docker.

## Features

- **Natural language time parsing** — English and Chinese
- **Persistent storage** — SQLite, survives reboots
- **Inline actions** — Done, Snooze, Delete without leaving the chat
- **Access control** — restrict to your own Telegram ID
- **Multi-platform** — Telegram + Slack run side by side, reminders fan out to every front-end you're on
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
help by adding reminders on your behalf. Everything
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
| `/invite`, `/revoke` | ❌ | ✅ |
| `/leave` | ✅ steps down | n/a |
| `/whoami` | ✅ shows their owner | ✅ shows secretaries |

Reminder cards delivered to the owner are prefixed `(via Alice)` so you
always know who added what; `/list` shows the same attribution.

### Revoking

```
/revoke 877247421
```

Revocation is a soft-delete: the secretary loses access immediately and
any still-active reminders they created are cancelled (the rows are
kept in the database with status `cancelled`, but stop firing and drop
out of `/list`).

### Disabling invites

Set `telegram.allow_invites: false` in `config.yaml` and restart. The
`/invite` command will refuse for everyone.

## Slack

Run Slack alongside Telegram so the same data and reminders are
reachable from both. Reminders fan out — if you're online in both
places, you get pinged in both places. The Telegram invite flow remains
the only way to add a new secretary; once invited, they can be mirrored
into Slack via the `user_map`.

### Setup

1. Visit [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From an app manifest**. Paste the contents of [`docs/slack-manifest.yaml`](docs/slack-manifest.yaml).
2. **Basic Information → App-Level Tokens** → generate a token with the `connections:write` scope. Copy as `slack.app_token` (starts with `xapp-`).
3. **Install App** → install to workspace. Copy the **Bot User OAuth Token** as `slack.bot_token` (starts with `xoxb-`).
4. For each user who should be able to use the Slack side: open their Slack profile → ⋮ → **Copy member ID** (e.g. `U01ABCDEF`). Map it to their canonical chat_id (= their Telegram chat_id) in `config.yaml`:

   ```yaml
   platforms: [telegram, slack]

   slack:
     bot_token: "xoxb-..."
     app_token: "xapp-..."
     user_map:
       U01ABCDEF: "123456789"   # Slack user → Telegram chat_id
   ```

5. Restart the bot. You should see:

   ```
   Starting platforms: ['telegram', 'slack']
   Slack bot starting (socket mode)...
   ```

### What works on Slack

| Feature | Slack |
|---|---|
| Free-text reminder capture (DM the bot) | ✅ |
| All slash commands (`/list`, `/whoami`, `/members`, `/revoke`, `/leave`, `/help`) | ✅ |
| Inline buttons (Done / Snooze / Delete) | ✅ Block Kit buttons |
| Owner notifications (invite accepted, secretary left) | ✅ DM to owner on every platform they're on |
| `/invite` | Generates a Telegram deep-link only (v1 — invites stay Telegram-native) |

### Known v1 limitations

- **Stale buttons across platforms.** If you're on Telegram + Slack, a reminder fires on both. Clicking ✅ Done on one does *not* edit the duplicate card on the other; a later Snooze on the stale copy is rejected ("already closed"). Fixing this requires tracking `(platform, channel, ts)` per send — punted to v2.
- **Slack-only owners are not supported.** Canonical chat_ids must be Telegram-numeric. To add a Slack-only secretary, invite them through Telegram first (they don't need to *use* Telegram afterwards), then map them in `user_map`.
- **Slack rate limits.** A burst of reminders to the same user is bounded by Slack's ~1 msg/sec per channel; the bot retries on 429 once per send. Sustained fan-out at scale is out of scope.

## Development

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src python -m pytest tests -q
```

Tests cover the message router end to end (capture, commands, buttons,
secretary scoping) against a real SQLite file, the scheduler across a
restart, the time parser, the secretary/invite lifecycle, the DB
migration, and the Slack adapter and formatter.

## Supported Time Expressions

Clock times are read in your configured `timezone`. A day named without
a time means 9am; a clock time that has already passed today means
tomorrow.

**English:**
- `at 4pm`, `by 3pm`, `at 10:30`, `at noon`
- `in 2 hours`, `in 30 minutes`, `in 5 mins`, `in half an hour`
- `tomorrow`, `tomorrow 9am`, `tonight at 8`
- `next Monday`, `on Friday`, `next week`, `at 4pm on Friday`
- `on 5 Dec`, `December 12 at 3pm`
- `remind me to X [time]`, `remind me [time] to X`

**Chinese (Traditional and Simplified):**
- `明天下午4點` (tomorrow 4pm), `今天早上9點` (today 9am), `下午3點`, `3點半`
- `5分鐘後` (in 5 minutes), `1小時後` (in 1 hour), `2天後` (in 2 days)
- `提醒我...`, `幫我提醒...`, `提醒一下...`

Without a `remind me` / `提醒我` phrase, an English time is only
recognised at the **end** of the message (`Call dentist at 4pm`), so
ordinary notes aren't misread. Anything with no time repeats every
`default_interval_hours`.

## Configuration Reference

```yaml
platforms:                  # list — runs all listed platforms concurrently
  - telegram                # legacy `platform: telegram` form still accepted
  # - slack

telegram:
  token: "..."              # from @BotFather
  allowed_chat_ids:         # leave empty to allow all users
    - 123456789

slack:                      # optional — only required when slack is in platforms
  bot_token: "xoxb-..."
  app_token: "xapp-..."
  user_map:
    U01ABCDEF: "123456789"  # Slack user id → canonical (Telegram) chat_id

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
    name = "myplatform"

    def __init__(self, cfg, on_message): ...
    def identity_for(self, canonical_chat_id: str) -> str | None: ...   # None = can't reach this user
    async def send_html(self, canonical_chat_id: str, text: str) -> None: ...
    async def send_reminder(self, canonical_chat_id: str, content: str, reminder_id: int) -> None: ...
    async def wait_ready(self) -> None: ...   # optional: return once sends will work
    async def run(self) -> None: ...          # listen forever
```

   Incoming messages go to `await on_message(canonical_chat_id, text, first_name)`;
   button presses go in as `/callback done:<id>`, `/callback delete:<id>` or
   `/callback snooze:<hours>:<id>`. The reply is either an HTML string
   (empty = stay silent) or `{"kind": "reminder_cards", "header": ..., "items": [...]}`.

2. Register it in `_instantiate()` in `src/platforms/__init__.py`
3. Add a config dataclass in `src/config.py` and credentials to `config.example.yaml`
4. Add `<name>` to `platforms:` in your `config.yaml`

## Project Structure

```
├── src/
│   ├── main.py              # central router (reminders, commands, callbacks)
│   ├── config.py            # config loader
│   ├── database.py          # SQLAlchemy models (Thought / Reminder / Secretary
│   │                        #   / Invite / SecretaryEvent)
│   ├── migrations.py        # idempotent SQLite migrations (ALTER TABLE + backup)
│   ├── secretary.py         # resolve_owner, invite lifecycle, format_actor, audit
│   ├── scheduler.py         # APScheduler wrapper (persistent jobs, UTC)
│   ├── time_parser.py       # time extraction (EN + ZH)
│   ├── dispatcher.py        # fans cross-platform sends out (reminders, owner notifs)
│   └── platforms/
│       ├── base.py          # abstract platform interface
│       ├── telegram.py      # Telegram implementation
│       ├── slack.py         # Slack implementation (Socket Mode + Block Kit)
│       └── _slack_format.py # HTML → Slack mrkdwn conversion
├── tests/                   # pytest suite
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

The sales-opportunity tracker (`/opp`) has been removed. Databases that
used it still contain the `opportunities` and `opportunity_updates`
tables; the bot no longer reads or writes them and never drops them. If
you want the space back: `sqlite3 data/reminders.db "DROP TABLE
opportunity_updates; DROP TABLE opportunities;"`.

## Tech Stack

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v21
- [APScheduler](https://apscheduler.readthedocs.io/) v3 — job persistence via SQLite
- [SQLAlchemy](https://www.sqlalchemy.org/) v2 — ORM
- [dateparser](https://dateparser.readthedocs.io/) — fallback for dates the built-in patterns don't cover
- Docker + Docker Compose

## License

MIT
