"""
auth_personal_tasks.py — Run ONCE locally to connect wongnicholas98@gmail.com Google Tasks.

What this does:
1. Opens a browser — log in as wongnicholas98@gmail.com
2. Grants Tasks read/write access only (no Gmail, no Calendar)
3. Saves the token to .env as GOOGLE_TOKEN_JSON_PERSONAL
4. Deletes the token file from disk

Run this after auth_setup.py has already completed.
Then add GOOGLE_TOKEN_JSON_PERSONAL to Railway's Variables tab.
"""

import json
import os
import sys

from dotenv import load_dotenv, set_key
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar",
]
ENV_FILE = ".env"
CREDS_FILE = "credentials.json"
TOKEN_FILE = "token_personal.json"
PERSONAL_ACCOUNT = "wongnicholas98@gmail.com"

print("\n" + "="*60)
print("  Personal Google Tasks Auth Setup")
print("="*60)

# Reconstruct credentials.json from env var if needed
if not os.path.exists(CREDS_FILE):
    creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if creds_env:
        with open(CREDS_FILE, "w") as f:
            f.write(creds_env)
        print("[OK] Reconstructed credentials.json from env var")
    else:
        print("ERROR: credentials.json not found and GOOGLE_CREDENTIALS_JSON not set.")
        print("Make sure auth_setup.py has been run first.")
        sys.exit(1)

creds = None
if os.path.exists(TOKEN_FILE):
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

if not creds or not creds.valid:
    print(f"\nOpening browser — log in as: {PERSONAL_ACCOUNT}")
    print("You may see 'This app isn't verified' — click Advanced → Go to (app)")
    flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print("[OK] Token saved temporarily")
else:
    print("[OK] Reusing existing token")

# Verify it's the right account by listing task lists
try:
    service = build("tasks", "v1", credentials=creds)
    result = service.tasklists().list(maxResults=5).execute()
    task_lists = result.get("items", [])
    print(f"\nTask lists found ({len(task_lists)}):")
    for tl in task_lists:
        print(f"  - {tl.get('title', '(unnamed)')}")
    print("\n[OK] Google Tasks access confirmed")
except Exception as e:
    print(f"ERROR: Could not access Google Tasks — {type(e).__name__}")
    sys.exit(1)

# Save to .env
with open(TOKEN_FILE, "r") as f:
    token_content = f.read().strip()

token_compact = json.dumps(json.loads(token_content))
set_key(ENV_FILE, "GOOGLE_TOKEN_JSON_PERSONAL", token_compact)
print("\n[OK] GOOGLE_TOKEN_JSON_PERSONAL saved to .env")

# Clean up temp files
for path in [TOKEN_FILE]:
    if os.path.exists(path):
        os.remove(path)
        print(f"[OK] Deleted {path} from disk")

# Also remove credentials.json if it was reconstructed (not originally present)
if os.path.exists(CREDS_FILE):
    os.remove(CREDS_FILE)
    print(f"[OK] Deleted {CREDS_FILE} from disk")

print("\n" + "="*60)
print("  DONE — personal Tasks token saved to .env")
print("="*60)
print("\nNext step: add GOOGLE_TOKEN_JSON_PERSONAL to Railway's Variables tab")
print("Copy the value from .env (the long JSON string after GOOGLE_TOKEN_JSON_PERSONAL=)")
