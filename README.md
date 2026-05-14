# Briefing Bot

A personal Telegram bot deployed on Railway that delivers structured daily briefings and manages tasks, synced with Gmail and Google Calendar.

## What it does

- Sends a **morning briefing** at 9 AM SGT and an **evening prep** at 7 PM SGT every day
- Reads Gmail (`nickwys@sph.com.sg`) for urgent/normal emails
- Reads 7 Google Calendars (work + personal) with conflict detection
- Manages an encrypted task list via Telegram commands, synced with Google Tasks
- Schedules gym sessions on the personal calendar with a Push/Pull/Legs/Rest rotation

## Telegram commands

| Command | Description |
|---------|-------------|
| `/start` | Confirm bot is online |
| `/morning` | Trigger morning briefing manually |
| `/evening` | Trigger evening briefing manually |
| `/weekend` | Personal-only schedule (no work content) |
| `/tasks` | View full task list |
| `/add [task]` | Add personal task (synced to Google Tasks) |
| `/add w [task] p0-3` | Add work task with priority |
| `/done` | Dropdown to mark any task done |
| `/done [task]` | Mark personal task done by name |
| `/done w [task]` | Mark work task done by name |
| `/append [task] + [text]` | Append text to a personal task |
| `/append w [task] + [text]` | Append text to a work task |
| `/update` | Dropdown: Done / Today / +1 day per task |
| `/update old > new` | Rename a task |
| `/update [task] p0-3` | Change work task priority |
| `/days MON WED FRI` | Schedule gym sessions for next week |
| `/skip` | Skip today's gym session |
| `/status` | This week's gym sessions |
| `/next` | Show next split without changing state |
| `/reschedule [date] [time]` | Move gym session to a specific date/time |
| `/setsplit [date] [Push/Pull/Legs/Rest]` | Override a day's split |
| `/help` | Full command list |

## Project structure

| File | Purpose |
|------|---------|
| `briefing_engine.py` | Gmail + Calendar fetching, briefing formatting, encrypted task management |
| `telegram_bot.py` | Telegram command handlers with security hardening |
| `gym_engine.py` | Gym session scheduling and split rotation |
| `scheduler_daemon.py` | Always-on daemon: runs bot polling, fires scheduled briefings, hosts health check |
| `auth_setup.py` | One-time local script to authenticate the work Google account |
| `auth_personal_tasks.py` | One-time local script to authenticate the personal Google account |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Container definition for Railway |
| `Procfile` | Railway process entry point |

## Data sources

| Source | Account | When |
|--------|---------|------|
| Gmail | nickwys@sph.com.sg | Every briefing |
| Work Calendar | nickwys@sph.com.sg | Weekdays only |
| Gym / Personal / Others / Ryan Chia / Vacation calendars | wongnicholas98@gmail.com | Every day |
| Google Tasks (work) | nickwys@sph.com.sg | Every briefing + /tasks |
| Google Tasks (personal) | wongnicholas98@gmail.com | Every briefing + /tasks |

## Setup

### Prerequisites

- Python 3.12+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Google Cloud project with Gmail API, Google Calendar API, and Google Tasks API enabled

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file (never commit this):

```
TELEGRAM_BOT_TOKEN=     # From @BotFather
TELEGRAM_CHAT_ID=       # Set by auth_setup.py
GOOGLE_CREDENTIALS_JSON=# OAuth client config (compact JSON)
GOOGLE_TOKEN_JSON=      # Work account OAuth token (compact JSON)
GOOGLE_TOKEN_JSON_PERSONAL= # Personal account OAuth token (compact JSON)
ENCRYPTION_KEY=         # Fernet key for encrypted task storage
TZ=Asia/Singapore
```

Generate the encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Google OAuth setup

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project and enable: **Gmail API**, **Google Calendar API**, **Google Tasks API**
3. Create an OAuth 2.0 Client ID (Desktop App type)
4. On the OAuth consent screen, add these scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/calendar.readonly`
   - `https://www.googleapis.com/auth/tasks`
5. Add both Google accounts as test users
6. Download the credentials file as `credentials.json`

### 4. Authenticate

Run the work account auth (opens a browser):

```bash
python auth_setup.py
```

Run the personal account auth (opens a browser):

```bash
python auth_personal_tasks.py
```

Both scripts save tokens to `.env` and delete the local credential files.

> **Note:** If your OAuth app is in **Testing** mode, refresh tokens expire after 7 days. To avoid re-authenticating weekly, publish the app on the OAuth consent screen.

## Deploy to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. Railway auto-detects the Dockerfile
4. In the service **Variables** tab, add all env vars from your `.env`
5. In **Settings → Networking**, set health check path `/` on port `8080`
6. Railway auto-deploys on every push to `master`

> **Persistent storage:** Railway's free tier has no persistent disk. The encrypted task file (`task_state.json.enc`) resets on redeploy. Tasks synced to Google Tasks are unaffected. Upgrade to Hobby tier ($20/month) for persistent volumes.

## Security

- Chat ID whitelist enforced on every message — unauthorized IDs are silently dropped
- Rate limiting: 3-second cooldown per chat ID
- Replay attack prevention via update ID tracking
- All user input sanitised and length-limited
- Task data encrypted at rest with Fernet symmetric encryption
- OAuth tokens never written to disk on the deployed instance
- No personal data in logs (timestamps and action codes only)
