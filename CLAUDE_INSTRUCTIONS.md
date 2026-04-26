# Daily Briefing Bot — Complete Build Instructions

## What this bot does

A Python daemon deployed on Railway that:
- Sends a structured briefing to Telegram at 9:00 AM and 5:00 PM SGT daily
- Reads Gmail (`nickwys@sph.com.sg`) and 7 Google Calendars
- Manages an encrypted task list via Telegram commands
- Applies weekday vs weekend rules for calendar sources

## Project files

| File | Purpose |
|------|---------|
| `briefing_engine.py` | Gmail + Calendar fetching, briefing formatting, encrypted task management |
| `telegram_bot.py` | Telegram command handlers with security hardening |
| `scheduler_daemon.py` | Always-on daemon: runs bot, fires scheduled jobs, hosts health check |
| `auth_setup.py` | One-time local auth script (not deployed) |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway process definition |
| `railway.json` | Railway deploy config |
| `.gitignore` | Files never committed to git |
| `.env` | Local secrets (never committed) |

## Data sources

| Source | Account | When used |
|--------|---------|-----------|
| Gmail | nickwys@sph.com.sg | Every briefing |
| Work Calendar | nickwys@sph.com.sg | Weekdays only |
| Gym Calendar | wongnicholas98@gmail.com (shared) | Every day |
| Others Calendar | wongnicholas98@gmail.com (shared) | Every day |
| Personal Calendar | wongnicholas98@gmail.com (shared) | Every day |
| Ryan Chia Calendar | wongnicholas98@gmail.com (shared) | Every day |
| Vacation Calendar | wongnicholas98@gmail.com (shared) | Every day |

## Calendar rules

- Weekdays: all 7 calendars
- Weekends: personal calendars only (Gym, Others, Personal, Ryan Chia, Vacation) — Work calendar excluded at API query level
- Ignore all-day events for conflict detection
- Gym blocks flagged as protected — never schedule over
- Vacation entries flagged prominently
- Ryan Chia entries explicitly flagged as joint commitments

## Environment variables (required)

```
TELEGRAM_BOT_TOKEN=     # From @BotFather
TELEGRAM_CHAT_ID=       # Auto-set by auth_setup.py
GOOGLE_CREDENTIALS_JSON=# OAuth client config (compact JSON string)
GOOGLE_TOKEN_JSON=      # OAuth access token (compact JSON string)
ENCRYPTION_KEY=         # Fernet key for task_state.json encryption
TZ=Asia/Singapore       # Set this on Railway dashboard
```

## One-time setup (run locally)

### 1. Install Python 3.12+

Download from https://www.python.org/downloads/windows/
Tick "Add Python to PATH" during install.

### 2. Install libraries

```bash
cd briefing-bot
pip install -r requirements.txt
```

### 3. Generate encryption key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste output into `.env` as `ENCRYPTION_KEY=...`

### 4. Set up Google OAuth

1. Go to https://console.cloud.google.com
2. Create new project (name it anything, e.g. "briefing-bot")
3. Enable: Gmail API + Google Calendar API
4. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
5. Application type: **Desktop App**
6. OAuth consent screen → Add scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/calendar.readonly`
7. Under "Test users", add `nickwys@sph.com.sg` and `wongnicholas98@gmail.com`
8. Download credentials → save as `credentials.json` in project folder

### 5. Run auth setup

```bash
python auth_setup.py
```

This opens a browser, authenticates, tests Gmail + Calendar, sends a Telegram test,
and saves all credentials to `.env`. It then deletes `credentials.json` and `token.json`.

After this step, `.env` should have all 5 values populated.

## Deploy to Railway

### Prerequisites

```bash
npm install -g @railway/cli
```

### Deploy steps

```bash
# In the briefing-bot folder:
git init
git add .
git commit -m "Initial briefing bot deployment"
railway login
railway init
railway variables set TELEGRAM_BOT_TOKEN=<value>
railway variables set TELEGRAM_CHAT_ID=<value>
railway variables set GOOGLE_CREDENTIALS_JSON=<value>
railway variables set GOOGLE_TOKEN_JSON=<value>
railway variables set ENCRYPTION_KEY=<value>
railway variables set TZ=Asia/Singapore
railway variables   # Confirm all 6 are set
railway up
railway logs        # Watch for startup confirmation
```

### Railway dashboard settings

- Health check path: `/`
- Health check port: `8080`
- Health check interval: 5 minutes
- Auto-restart on failure: enabled

## Telegram commands

| Command | Action |
|---------|--------|
| `/start` | Confirm online, list commands |
| `/morning` | Manual morning briefing |
| `/evening` | Manual evening briefing |
| `/tasks` | Full task list |
| `/done [task]` | Mark task done (fuzzy match) |
| `/add [task]` | Add task (max 200 chars) |
| `/update [text]` | Log completions/new tasks/notes |
| `/weekend` | Personal schedule only |
| `/help` | Command list |

## Security checklist

- [ ] No secrets in any Python file
- [ ] `.env` in `.gitignore`
- [ ] `task_state.json.enc` encrypted with Fernet key
- [ ] Chat ID whitelist enforced on every message
- [ ] Unauthorized chat IDs silently dropped, logged
- [ ] Rate limiting: 3-second cooldown per chat ID
- [ ] Replay attack prevention: duplicate update_ids rejected
- [ ] Input sanitisation on all user inputs
- [ ] No personal data in logs (timestamps + action names only)
- [ ] Health check returns `200 OK` only
- [ ] Credential files never persisted to Railway disk

## Maintenance

- Monthly: send a message from a different Telegram account — confirm silent drop
- If token expires: re-run `auth_setup.py` locally, update Railway env vars
- Log rotation: automatic at 10MB, last 3 files kept

## Railway free tier notes

- Free tier: $5/month credit
- Worker process + health check pings should keep it alive
- Monitor usage in Railway dashboard → Usage tab
- If approaching limit, reduce briefing frequency or upgrade to Hobby tier ($20/month)
