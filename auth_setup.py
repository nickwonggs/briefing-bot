"""
auth_setup.py — Run ONCE locally to authenticate Google + Telegram.

What this script does (plain English):
1. Opens a browser so you log in to nickwys@sph.com.sg
2. Saves the OAuth token locally as token.json
3. Tests Gmail: lists 5 recent email subjects
4. Tests Calendar: lists all calendar names (verify all 7 appear)
5. Reads your Telegram Bot Token from .env, sends a test message,
   and auto-saves the resulting Chat ID to .env
6. Encodes credentials.json and token.json as single-line strings
   and saves them to .env as GOOGLE_CREDENTIALS_JSON / GOOGLE_TOKEN_JSON
7. Deletes credentials.json and token.json from disk

After this script succeeds you do NOT need to run it again unless your
OAuth token expires and cannot be refreshed (very rare).
"""

import json
import os
import sys
import base64
import asyncio
import re

# Load .env before anything else
from dotenv import load_dotenv, set_key
load_dotenv()

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks",
]
REQUIRED_ACCOUNT = "nickwys@sph.com.sg"
ENV_FILE = ".env"
CREDS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


def _separator(title: str) -> None:
    print(f"\n{'-'*60}")
    print(f"  {title}")
    print(f"{'-'*60}")


# ── Step 1: Google OAuth ───────────────────────────────────────────────────────

_separator("STEP 1: Google OAuth Login")

creds = None
if os.path.exists(TOKEN_FILE):
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

if not creds or not creds.valid:
    # Reconstruct credentials.json from env var if it was previously deleted
    if not os.path.exists(CREDS_FILE):
        creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
        if creds_env:
            with open(CREDS_FILE, "w") as f:
                f.write(creds_env)
            print("[OK] Reconstructed credentials.json from GOOGLE_CREDENTIALS_JSON env var")
        else:
            print(f"ERROR: {CREDS_FILE} not found and GOOGLE_CREDENTIALS_JSON not set.")
            print("Download credentials from Google Cloud Console -> APIs & Services -> Credentials")
            print("Then place it here:", os.path.abspath(CREDS_FILE))
            sys.exit(1)
    print("Opening browser for Google login...")
    print(f"  -> Log in as: {REQUIRED_ACCOUNT}")
    print("  -> You may see a 'This app isn't verified' warning")
    print("     Click 'Advanced' -> 'Go to (app)' to proceed")
    flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"[OK] Token saved to {TOKEN_FILE}")
else:
    print("[OK] Reusing existing token (no browser login needed)")


# ── Step 2: Validate account ───────────────────────────────────────────────────

_separator("STEP 2: Validate authenticated account")

gmail_service = build("gmail", "v1", credentials=creds)
profile = gmail_service.users().getProfile(userId="me").execute()
authenticated_email = profile.get("emailAddress", "")

print(f"Authenticated as: {authenticated_email}")
if authenticated_email.lower() != REQUIRED_ACCOUNT.lower():
    print(f"ERROR: Expected {REQUIRED_ACCOUNT} but got {authenticated_email}")
    print("Please log out of all Google accounts in the browser window and retry.")
    sys.exit(1)
print(f"[OK] Account confirmed: {authenticated_email}")


# ── Step 3: Gmail test ─────────────────────────────────────────────────────────

_separator("STEP 3: Gmail access test")

results = gmail_service.users().messages().list(userId="me", maxResults=5).execute()
messages = results.get("messages", [])

if not messages:
    print("No emails found (inbox may be empty). Continuing...")
else:
    print("5 most recent email subjects:")
    for msg_ref in messages:
        msg = gmail_service.users().messages().get(
            userId="me",
            id=msg_ref["id"],
            format="metadata",
            metadataHeaders=["Subject", "From"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        print(f"  -{headers.get('Subject', '(no subject)')[:80]}")
print("[OK] Gmail access confirmed")


# ── Step 4: Calendar test ──────────────────────────────────────────────────────

_separator("STEP 4: Calendar access test")

cal_service = build("calendar", "v3", credentials=creds)
cal_list = cal_service.calendarList().list().execute()
calendars = cal_list.get("items", [])

print("Calendars found:")
for cal in calendars:
    print(f"  -{cal.get('summary', '(unnamed)')} — {cal.get('id', '')}")

print(f"\nTotal: {len(calendars)} calendars")
print("\nExpected calendars (verify these appear above):")
expected = [
    "Work/nickwys@sph.com.sg",
    "Gym",
    "Personal",
    "Others",
    "Ryan Chia",
    "Vacation",
    "wongnicholas98@gmail.com (personal account)",
]
for e in expected:
    print(f"  [ ]{e}")

print("\nIf any are missing: go to calendar.google.com -> Settings -> check 'Other calendars'")
print("[OK] Calendar access confirmed")


# ── Step 5: Telegram test ──────────────────────────────────────────────────────

_separator("STEP 5: Telegram bot test")

bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not bot_token or bot_token == "your_token_here":
    print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
    print("Add your BotFather token to .env and rerun this script.")
    sys.exit(1)


async def _send_test():
    import time
    from telegram import Bot
    bot = Bot(token=bot_token)
    print(f"\nBot token detected: {bot_token[:10]}...")
    print("\nAction required:")
    print("  1. Open Telegram")
    print("  2. Search for your bot by username")
    print("  3. Send it any message (e.g. 'hi')")
    print("\nWaiting up to 60 seconds for your message...")

    chat_id = None
    offset = None
    deadline = time.time() + 90
    while time.time() < deadline:
        updates = await bot.get_updates(offset=offset, timeout=10)
        for upd in updates:
            offset = upd.update_id + 1
            if upd.message and upd.message.chat_id:
                chat_id = upd.message.chat_id
                break
        if chat_id:
            break

    if not chat_id:
        print("No message received within 60 seconds.")
        print("Make sure you sent a message to the bot in Telegram and rerun this script.")
        return None

    print(f"Detected Chat ID: {chat_id}")
    await bot.send_message(
        chat_id=chat_id,
        text="Daily Briefing Bot auth test successful! Bot is connected and ready.",
    )
    print(f"[OK] Test message sent to Chat ID: {chat_id}")
    return chat_id


chat_id = asyncio.run(_send_test())
if chat_id is None:
    print("Telegram test failed. Check your bot token and try again.")
    sys.exit(1)


# ── Step 6: Save to .env ───────────────────────────────────────────────────────

_separator("STEP 6: Saving credentials to .env")

# Encode credentials.json and token.json as single-line strings
with open(CREDS_FILE, "r") as f:
    creds_content = f.read().strip()

with open(TOKEN_FILE, "r") as f:
    token_content = f.read().strip()

# Compact JSON (remove whitespace) so it's a clean single-line env var
creds_compact = json.dumps(json.loads(creds_content))
token_compact = json.dumps(json.loads(token_content))

set_key(ENV_FILE, "GOOGLE_CREDENTIALS_JSON", creds_compact)
set_key(ENV_FILE, "GOOGLE_TOKEN_JSON", token_compact)
set_key(ENV_FILE, "TELEGRAM_CHAT_ID", str(chat_id))

print("[OK] GOOGLE_CREDENTIALS_JSON saved to .env")
print("[OK] GOOGLE_TOKEN_JSON saved to .env")
print(f"[OK] TELEGRAM_CHAT_ID={chat_id} saved to .env")


# ── Step 7: Delete credential files from disk ──────────────────────────────────

_separator("STEP 7: Cleaning up disk files")

for path in [CREDS_FILE, TOKEN_FILE]:
    if os.path.exists(path):
        os.remove(path)
        print(f"[OK] Deleted {path} from disk (values preserved in .env)")

print("\n" + "="*60)
print("  AUTH SETUP COMPLETE")
print("="*60)
print("\nAll credentials are stored in .env only.")
print("Never commit .env to git.")
print("\nNext step: run Part 2 to generate the encryption key, then")
print("build and test the bot files.")
print("\nTo generate your ENCRYPTION_KEY, run:")
print('  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
print("Then paste the output into .env as ENCRYPTION_KEY=...")
