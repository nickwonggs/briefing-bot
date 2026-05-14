"""
loyalty_lobby.py — LoyaltyLobby.com RSS digest.

Fetches articles published between 15:00 SGT yesterday and 15:00 SGT today,
summarises each one with Groq (llama-3.1-8b-instant), and sends the digest
to Telegram. Priority tags: ACCOR and SQ.
"""

import asyncio
import logging
import os

import feedparser
import pytz
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

log = logging.getLogger("loyalty_lobby")

_FEED_URL = "https://loyaltylobby.com/feed"
_SGT = pytz.timezone("Asia/Singapore")
_TIMEOUT = 15  # seconds for HTTP requests

_PRIORITY_RULES = [
    ("ACCOR", ["accor", "all accor"]),
    ("SQ",    ["singapore airlines", "krisflyer", "sq"]),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _window() -> tuple[datetime, datetime]:
    """Return (start, end) as SGT-aware datetimes: 15:00 yesterday → 15:00 today."""
    now_sgt = datetime.now(_SGT)
    end   = now_sgt.replace(hour=15, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    return start, end


def _priority_tag(title: str) -> str | None:
    """Return 'ACCOR', 'SQ', or None based on keywords in the title."""
    lower = title.lower()
    for tag, keywords in _PRIORITY_RULES:
        if any(kw in lower for kw in keywords):
            return tag
    return None


def _strip_html(raw: str) -> str:
    return BeautifulSoup(raw or "", "html.parser").get_text(" ", strip=True)


def _fetch_article_text(url: str) -> str | None:
    """Fetch the full article page and return the main body text, or None on failure."""
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # LoyaltyLobby uses <div class="entry-content"> for article body
        body = (
            soup.find("div", class_="entry-content")
            or soup.find("article")
            or soup.find("main")
        )
        if not body:
            return None
        # Remove script/style noise
        for tag in body(["script", "style", "figure", "aside"]):
            tag.decompose()
        text = body.get_text(" ", strip=True)
        return text[:4000] if text else None
    except Exception as exc:
        log.warning(f"[LOYALTY_LOBBY] [FETCH_FAIL] [{url}] [{type(exc).__name__}]")
        return None


def _summarise(title: str, body: str, fallback: str) -> str:
    """Summarise article text with Groq. Falls back to RSS description on any failure."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if api_key and body:
        try:
            from groq import Groq
            prompt = (
                "You are summarising a travel loyalty / airline / hotel points article "
                "for a frequent flyer who wants to know the key takeaway quickly.\n\n"
                f"Article title: {title}\n\n"
                f"Article body:\n{body}\n\n"
                "Write a 2–3 sentence summary. Be direct — no greetings, no sign-off."
            )
            client = Groq(api_key=api_key)
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            log.warning(f"[LOYALTY_LOBBY] [GROQ_FAIL] [{type(exc).__name__}] {exc}")
    return fallback


async def _send(bot_token: str, chat_id: int, text: str) -> None:
    from telegram import Bot
    bot = Bot(token=bot_token)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=None,
                           disable_web_page_preview=True)


# ── Public entry point ─────────────────────────────────────────────────────────

def send_digest() -> None:
    """Fetch, filter, summarise, and send the LoyaltyLobby digest."""
    log.info("[LOYALTY_LOBBY] [START]")

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id_str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id_str:
        log.error("[LOYALTY_LOBBY] [MISSING_ENV] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        log.error("[LOYALTY_LOBBY] [BAD_CHAT_ID]")
        return

    # ── Fetch feed ─────────────────────────────────────────────────────────────
    try:
        feed = feedparser.parse(_FEED_URL)
        if feed.bozo and not feed.entries:
            log.error(f"[LOYALTY_LOBBY] [FEED_FAIL] {feed.bozo_exception}")
            return
    except Exception as exc:
        log.error(f"[LOYALTY_LOBBY] [FEED_FAIL] [{type(exc).__name__}] {exc}")
        return

    start, end = _window()
    log.info(f"[LOYALTY_LOBBY] [WINDOW] {start.isoformat()} → {end.isoformat()}")

    # ── Filter by publish time ─────────────────────────────────────────────────
    articles = []
    for entry in feed.entries:
        try:
            published = datetime(*entry.published_parsed[:6], tzinfo=pytz.utc).astimezone(_SGT)
        except Exception:
            continue
        if not (start <= published < end):
            continue
        articles.append({
            "title":     entry.get("title", "Untitled").strip(),
            "url":       entry.get("link", ""),
            "published": published,
            "fallback":  _strip_html(entry.get("summary", "")),
        })

    if not articles:
        log.info("[LOYALTY_LOBBY] [NO_ARTICLES]")
        asyncio.run(_send(bot_token, chat_id,
                          "📰 LoyaltyLobby digest: no new articles in the last 24h."))
        return

    # ── Tag priority, fetch full text, summarise ───────────────────────────────
    priority = []
    standard = []

    for art in articles:
        tag = _priority_tag(art["title"])
        body = _fetch_article_text(art["url"])
        summary = _summarise(art["title"], body, art["fallback"])
        art["summary"] = summary
        art["tag"] = tag
        if tag:
            priority.append(art)
        else:
            standard.append(art)

    priority.sort(key=lambda a: a["published"], reverse=True)
    standard.sort(key=lambda a: a["published"], reverse=True)

    # ── Send header ────────────────────────────────────────────────────────────
    header = (
        f"📰 LoyaltyLobby Digest\n"
        f"{len(articles)} article{'s' if len(articles) != 1 else ''}"
        + (f", {len(priority)} priority" if priority else "")
    )
    asyncio.run(_send(bot_token, chat_id, header))

    # ── Send articles ──────────────────────────────────────────────────────────
    for art in priority + standard:
        if art["tag"]:
            first_line = f"🔴 [{art['tag']}] {art['title']}"
        else:
            first_line = f"⚪ {art['title']}"
        msg = f"{first_line}\n{art['summary']}\n👉 {art['url']}"
        try:
            asyncio.run(_send(bot_token, chat_id, msg))
        except Exception as exc:
            log.error(f"[LOYALTY_LOBBY] [SEND_FAIL] [{type(exc).__name__}] {exc}")

    log.info(f"[LOYALTY_LOBBY] [OK] [{len(priority)}_PRIORITY] [{len(standard)}_STANDARD]")
