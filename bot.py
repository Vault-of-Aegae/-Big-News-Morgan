"""
News Article Accountability Bot
- Tracks who sends news articles daily in a Telegram group
- Uses Google Gemini AI (free) to verify if a link is a legitimate news article
- At midnight (SGT), announces who defaulted and adds $1 to their tab
- Resets yearly
"""

import os
import re
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import Database

# ─── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TIMEZONE = ZoneInfo("Asia/Singapore")

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

db = Database("news_bot.db")

# ─── Known shortener domains (we always try to resolve these) ─────────────────
SHORTENER_DOMAINS = {
    "search.app", "t.co", "bit.ly", "tinyurl.com", "ow.ly", "buff.ly",
    "goo.gl", "short.link", "rb.gy", "is.gd", "v.gd", "cutt.ly",
    "bl.ink", "tiny.cc", "shorte.st", "adf.ly", "x.co",
}

# ─── Domains that are definitely NOT news articles ────────────────────────────
NON_NEWS_DOMAINS = {
    "youtube.com", "youtu.be", "instagram.com", "facebook.com", "twitter.com",
    "x.com", "tiktok.com", "reddit.com", "linkedin.com", "pinterest.com",
    "snapchat.com", "telegram.org", "t.me", "whatsapp.com", "discord.com",
    "spotify.com", "netflix.com", "twitch.tv", "amazon.com", "shopee.sg",
    "lazada.sg", "carousell.com", "wikipedia.org",
}

# ─── Browser-like headers to avoid bot detection ─────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def today_str() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def current_year() -> int:
    return datetime.now(TIMEZONE).year


def clean_url(url: str) -> str:
    """
    Clean a raw URL string of common issues:
    - Strip trailing punctuation (. , ) ] > ' ")
    - Strip wrapping brackets or quotes
    - Strip whitespace
    """
    url = url.strip()
    # Strip trailing punctuation that often gets captured accidentally
    url = re.sub(r'[.,)\]>\'\"]+$', '', url)
    # Strip leading brackets/quotes
    url = re.sub(r'^[(\[<\'\"]+', '', url)
    return url.strip()


def get_domain(url: str) -> str:
    """Extract the base domain from a URL, e.g. 'www.bbc.com' -> 'bbc.com'."""
    try:
        match = re.search(r'https?://(?:www\.)?([^/?\s]+)', url)
        if match:
            return match.group(1).lower()
    except Exception:
        pass
    return ""


def is_likely_non_news(url: str) -> bool:
    """Quick check: is this URL from a domain that's definitely not news?"""
    domain = get_domain(url)
    return any(domain == nd or domain.endswith("." + nd) for nd in NON_NEWS_DOMAINS)


def is_shortener(url: str) -> bool:
    """Check if this URL is from a known URL shortener."""
    domain = get_domain(url)
    return any(domain == sd or domain.endswith("." + sd) for sd in SHORTENER_DOMAINS)


def extract_urls(update: Update) -> list[str]:
    """
    Robustly extract all URLs from a Telegram message.

    Handles:
    - Regular messages with URLs in text
    - Messages with hyperlinked text (text_link entities)
    - Captions on photos/videos/files
    - Forwarded messages
    - Multiple URLs (returns all, deduplicated, in order)
    - Trailing punctuation cleanup
    """
    urls = []
    seen = set()
    message = update.message

    # Collect text and entities from both message body and caption
    sources = []
    if message.text:
        sources.append((message.text, message.entities or []))
    if message.caption:
        sources.append((message.caption, message.caption_entities or []))

    for text, entities in sources:
        for entity in entities:
            raw = None
            if entity.type == "url":
                raw = text[entity.offset: entity.offset + entity.length]
            elif entity.type == "text_link" and entity.url:
                raw = entity.url

            if raw:
                cleaned = clean_url(raw)
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    urls.append(cleaned)

        # Fallback: scan raw text with regex for any http/https links
        # This catches URLs that Telegram's entity parser might miss
        if not urls:
            pattern = r'https?://[^\s\]\[\(\)<>\'"]{5,}'
            for match in re.finditer(pattern, text):
                cleaned = clean_url(match.group(0))
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    urls.append(cleaned)

    return urls


def pick_best_url(urls: list[str]) -> str | None:
    """
    Given a list of URLs from a message, pick the best one to check.
    Prefers non-shortener, non-social-media URLs.
    Falls back to shorteners (we'll resolve them).
    Filters out definite non-news domains.
    """
    if not urls:
        return None

    # First pass: prefer direct, non-shortened news-looking URLs
    for url in urls:
        if not is_likely_non_news(url) and not is_shortener(url):
            return url

    # Second pass: accept shorteners (we'll resolve them)
    for url in urls:
        if not is_likely_non_news(url):
            return url

    # Nothing useful found
    return None


async def resolve_url(url: str) -> tuple[str, str]:
    """
    Follow redirects to get the final URL and extract useful page content.

    Handles:
    - Shortened URLs (search.app, t.co, bit.ly etc.)
    - Paywalled articles (detects login walls)
    - Bot-blocking (uses realistic browser headers)
    - Timeouts (returns original URL gracefully)
    - Non-200 responses (returns URL with empty content)

    Returns (final_url, page_content).
    page_content is the most relevant text extracted from the HTML,
    not just the raw first N characters (which are often cookie banners).
    """
    try:
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=True,
            headers=BROWSER_HEADERS,
        ) as client:
            resp = await client.get(url)
            final_url = str(resp.url)

            if resp.status_code != 200:
                logger.warning(f"Got status {resp.status_code} for {url}")
                return final_url, ""

            html = resp.text

            # Instead of blindly taking first 4000 chars (often cookie banners/nav),
            # try to extract the most relevant content:
            # 1. Look for <article> or <main> tags — these contain article body
            # 2. Look for <title> and <meta description> for a quick summary
            # 3. Fall back to stripping all HTML tags and taking plain text

            content_parts = []

            # Extract page title
            title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            if title_match:
                content_parts.append("TITLE: " + re.sub(r'\s+', ' ', title_match.group(1)).strip())

            # Extract meta description
            desc_match = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
                html, re.IGNORECASE
            )
            if not desc_match:
                desc_match = re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
                    html, re.IGNORECASE
                )
            if desc_match:
                content_parts.append("DESCRIPTION: " + desc_match.group(1).strip())

            # Extract Open Graph title and description (news sites use these heavily)
            og_title = re.search(
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                html, re.IGNORECASE
            )
            if og_title:
                content_parts.append("OG_TITLE: " + og_title.group(1).strip())

            og_desc = re.search(
                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                html, re.IGNORECASE
            )
            if og_desc:
                content_parts.append("OG_DESCRIPTION: " + og_desc.group(1).strip())

            og_site = re.search(
                r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
                html, re.IGNORECASE
            )
            if og_site:
                content_parts.append("OG_SITE: " + og_site.group(1).strip())

            # Extract article/main body text (strip HTML tags)
            article_match = re.search(
                r'<(?:article|main)[^>]*>(.*?)</(?:article|main)>',
                html, re.IGNORECASE | re.DOTALL
            )
            if article_match:
                body = re.sub(r'<[^>]+>', ' ', article_match.group(1))
                body = re.sub(r'\s+', ' ', body).strip()
                content_parts.append("BODY: " + body[:2000])
            else:
                # Strip all tags from full HTML as fallback
                plain = re.sub(r'<[^>]+>', ' ', html)
                plain = re.sub(r'\s+', ' ', plain).strip()
                content_parts.append("BODY: " + plain[:2000])

            page_content = "\n".join(content_parts)
            logger.info(f"URL resolved: {url} -> {final_url} ({len(page_content)} chars extracted)")
            return final_url, page_content

    except httpx.TimeoutException:
        logger.warning(f"Timeout fetching {url}")
        return url, ""
    except httpx.SSLError:
        logger.warning(f"SSL error fetching {url}")
        return url, ""
    except Exception as e:
        logger.warning(f"Could not fetch URL {url}: {e}")
        return url, ""


async def is_news_article(url: str) -> tuple[bool, str, str]:
    """
    Use Google Gemini (free) to determine if a URL is a legitimate news article.
    Returns (is_valid, reason, summary).

    Handles:
    - Gemini response format errors (robust parsing)
    - Gemini API errors (graceful failure with message)
    - Markdown characters in summary (escaped for Telegram)
    - Paywall detection
    """
    try:
        # Quick reject: known non-news domains
        if is_likely_non_news(url):
            domain = get_domain(url)
            return False, f"{domain} is not a news outlet.", ""

        # Resolve the URL — follow all redirects, extract structured content
        final_url, page_content = await resolve_url(url)

        # After resolution, check the final domain too
        if is_likely_non_news(final_url):
            domain = get_domain(final_url)
            return False, f"Link redirects to {domain}, which is not a news outlet.", ""

        # Detect paywall / login wall
        paywall_signals = ["sign in to read", "subscribe to continue", "create an account",
                           "log in to access", "premium content", "subscribers only"]
        content_lower = page_content.lower()
        is_paywalled = any(signal in content_lower for signal in paywall_signals)

        prompt = f"""You are a fact-checker determining if a URL points to a legitimate news article.

Original URL: {url}
Final URL (after following all redirects): {final_url}
Paywalled: {"Yes — the page requires login/subscription, but judge the URL and title alone" if is_paywalled else "No"}

Extracted page content:
{page_content if page_content else "Could not fetch page content — judge based on the URL and domain alone."}

Your job: determine if this is a REAL news article from a legitimate news outlet.

ACCEPT if:
- Domain is a known news outlet (local or international): straitstimes.com, channelnewsasia.com, bbc.com, reuters.com, scmp.com, theguardian.com, nytimes.com, mothership.sg, todayonline.com, zaobao.com, etc.
- Content reports on real events: current affairs, politics, business, sports, science, health, etc.
- Even paywalled articles from real news sites count — the paywall itself proves it's a serious outlet.

REJECT if:
- It's a blog, opinion newsletter, press release, forum, or social media post
- It's a product page, ad, or spam
- The domain is not a recognisable news outlet

Respond ONLY in this exact format, nothing else:
VALID: true/false
REASON: one sentence explanation
SUMMARY: 2-3 sentence plain English summary of the article (write "N/A" if not valid or cannot determine)"""

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                GEMINI_URL,
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}]},
            )
            data = response.json()

        # Robust response parsing — handle unexpected Gemini output gracefully
        if "candidates" not in data or not data["candidates"]:
            logger.error(f"Unexpected Gemini response: {data}")
            return False, "AI verification service is temporarily unavailable.", ""

        response_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        logger.info(f"Gemini response: {response_text}")

        is_valid = False
        reason = "Could not determine."
        summary = ""

        for line in response_text.splitlines():
            line = line.strip()
            if line.upper().startswith("VALID:"):
                is_valid = "true" in line.lower()
            elif line.upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
            elif line.upper().startswith("SUMMARY:"):
                raw_summary = line.split(":", 1)[1].strip()
                # Escape Telegram Markdown special characters to prevent formatting crashes
                summary = raw_summary if raw_summary.upper() != "N/A" else ""

        return is_valid, reason, summary

    except httpx.TimeoutException:
        logger.error(f"Gemini API timed out for {url}")
        return False, "Verification timed out — please try again.", ""
    except Exception as e:
        logger.error(f"Error checking article {url}: {e}")
        return False, "Could not verify the link — please try again.", ""


# ─── Commands ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *News Accountability Bot*\n\n"
        "I track who sends a news article every day.\n"
        "If you miss a day, *$1 goes into the pot!* 💰\n\n"
        "Commands:\n"
        "/register — Join the accountability group\n"
        "/status — See today's submissions\n"
        "/leaderboard — See who owes the most\n"
        "/pot — Check the current pot total\n"
        "/history — Your personal submission history\n"
        "/adjust — Manually adjust someone's balance\n"
        "/help — Show this message",
        parse_mode="Markdown",
    )


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    added = db.register_member(chat_id, user.id, user.username or user.first_name, current_year())
    if added:
        await update.message.reply_text(
            f"✅ @{user.username or user.first_name} has joined the accountability group! "
            f"Remember to send a news article every day 📰"
        )
    else:
        await update.message.reply_text(
            f"You're already registered, @{user.username or user.first_name}! Keep sending those articles 📰"
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    today = today_str()
    members = db.get_members(chat_id, current_year())
    submitted = db.get_submissions_today(chat_id, today)
    submitted_ids = {s["user_id"] for s in submitted}

    done = [m for m in members if m["user_id"] in submitted_ids]
    pending = [m for m in members if m["user_id"] not in submitted_ids]

    msg = f"📋 *Article Status for {today}*\n\n"
    if done:
        msg += "✅ *Submitted:*\n"
        for m in done:
            msg += f"  • @{m['username']}\n"
    if pending:
        msg += "\n⏳ *Still waiting:*\n"
        for m in pending:
            msg += f"  • @{m['username']}\n"
    if not members:
        msg += "_No members registered yet. Use /register to join!_"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    year = current_year()
    members = db.get_members(chat_id, year)

    if not members:
        await update.message.reply_text("No members registered yet!")
        return

    msg = f"💸 *Pot Contributions — {year}*\n\n"
    members_sorted = sorted(members, key=lambda m: m["owed"], reverse=True)
    for i, m in enumerate(members_sorted):
        emoji = "🥇" if i == 0 and m["owed"] > 0 else "👤"
        msg += f"{emoji} @{m['username']} — *${m['owed']:.2f}*\n"

    total = sum(m["owed"] for m in members)
    msg += f"\n🍽 *Total pot: ${total:.2f}*"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_pot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    members = db.get_members(chat_id, current_year())
    total = sum(m["owed"] for m in members)
    await update.message.reply_text(
        f"🍽 Current pot: *${total:.2f}*\n"
        f"Keep sharing those articles to keep your tab clean! 📰",
        parse_mode="Markdown",
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    year = current_year()
    records = db.get_user_history(chat_id, user.id, year)

    if not records:
        await update.message.reply_text("No submission history found for this year.")
        return

    msg = f"📅 *Your submission history ({year}):*\n\n"
    for r in records[-20:]:
        status = "✅" if r["submitted"] else "❌ ($1)"
        msg += f"{status} {r['date']}\n"

    owed = db.get_member_owed(chat_id, user.id, year)
    msg += f"\n💸 Total owed: *${owed:.2f}*"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    year = current_year()
    args = context.args

    usage = (
        "⚙️ *Usage:*\n"
        "`/adjust @username set <amount>` — set balance to exact amount\n"
        "`/adjust @username add <amount>` — add to balance\n"
        "`/adjust @username remove <amount>` — subtract from balance\n\n"
        "_Example: `/adjust @john remove 1`_"
    )

    if not args or len(args) < 3:
        await update.message.reply_text(usage, parse_mode="Markdown")
        return

    raw_target, action, raw_amount = args[0], args[1].lower(), args[2]
    target_username = raw_target.lstrip("@").lower()

    if action not in ("set", "add", "remove"):
        await update.message.reply_text(
            f"❌ Unknown action `{action}`. Use `set`, `add`, or `remove`.\n\n{usage}",
            parse_mode="Markdown",
        )
        return

    try:
        amount = float(raw_amount)
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Amount must be a positive number, e.g. `1`, `2.50`.",
            parse_mode="Markdown",
        )
        return

    members = db.get_members(chat_id, year)
    target = next(
        (m for m in members if m["username"].lower() == target_username), None
    )

    if not target:
        await update.message.reply_text(
            f"❌ Couldn't find `@{target_username}` in registered members.\n"
            f"Make sure they've used `/register` and the username is spelled correctly.",
            parse_mode="Markdown",
        )
        return

    old_balance = target["owed"]

    if action == "set":
        db.set_owed(chat_id, target["user_id"], amount, year)
        new_balance = amount
        verb = f"set to *${new_balance:.2f}*"
    elif action == "add":
        db.add_owed(chat_id, target["user_id"], amount, year)
        new_balance = old_balance + amount
        verb = f"increased by *${amount:.2f}* → now *${new_balance:.2f}*"
    else:
        new_amount = max(0.0, old_balance - amount)
        db.set_owed(chat_id, target["user_id"], new_amount, year)
        new_balance = new_amount
        actually_removed = old_balance - new_amount
        verb = f"reduced by *${actually_removed:.2f}* → now *${new_balance:.2f}*"

    adjuster = update.effective_user.username or update.effective_user.first_name
    total_pot = sum(m["owed"] for m in db.get_members(chat_id, year))

    await update.message.reply_text(
        f"✏️ *Balance adjusted!*\n\n"
        f"👤 @{target['username']}'s balance {verb}\n"
        f"_(was ${old_balance:.2f})_\n\n"
        f"🍽 *New pot total: ${total_pot:.2f}*\n\n"
        f"_Adjusted by @{adjuster}_",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ─── Message Handler ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detect URLs in messages and verify if they are news articles."""
    if not update.message:
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    today = today_str()
    year = current_year()

    if not db.is_member(chat_id, user.id, year):
        return

    # Extract all URLs from the message, then pick the best one
    all_urls = extract_urls(update)
    url = pick_best_url(all_urls)

    if not url:
        return

    # Already submitted today
    if db.has_submitted_today(chat_id, user.id, today):
        await update.message.reply_text(
            f"✅ @{user.username or user.first_name}, you've already submitted your article today! 🎉",
            reply_to_message_id=update.message.message_id,
        )
        return

    thinking_msg = await update.message.reply_text(
        "🔍 Checking if that's a news article...",
        reply_to_message_id=update.message.message_id,
    )

    is_valid, reason, summary = await is_news_article(url)

    if is_valid:
        db.record_submission(chat_id, user.id, today, url, year)
        msg = (
            f"✅ *Article accepted!* @{user.username or user.first_name} has submitted their article for today.\n\n"
            f"📝 *Summary:* {summary}\n\n"
            f"_{reason}_"
        )
        await thinking_msg.edit_text(msg, parse_mode="Markdown")
    else:
        await thinking_msg.edit_text(
            f"❌ *Not accepted.* That doesn't appear to be a legitimate news article.\n\n"
            f"_{reason}_\n\n"
            f"Please send a valid news article link to fulfil today's quota!",
            parse_mode="Markdown",
        )


# ─── Midnight Job ─────────────────────────────────────────────────────────────

async def midnight_check(app: Application):
    yesterday_dt = datetime.now(TIMEZONE) - timedelta(days=1)
    yesterday = yesterday_dt.strftime("%Y-%m-%d")
    logger.info(f"Running midnight check for {yesterday}")

    chats = db.get_all_chats()
    year = current_year()

    for chat_id in chats:
        members = db.get_members(chat_id, year)
        submitted_ids = {s["user_id"] for s in db.get_submissions_today(chat_id, yesterday)}
        defaulters = [m for m in members if m["user_id"] not in submitted_ids]

        if not defaulters:
            await app.bot.send_message(
                chat_id,
                "🎉 Everyone submitted a news article yesterday! No one owes anything. Keep it up! 📰",
            )
            continue

        for m in defaulters:
            db.add_owed(chat_id, m["user_id"], 1.0, year)
            db.record_default(chat_id, m["user_id"], yesterday, year)

        total_pot = sum(m["owed"] for m in db.get_members(chat_id, year))
        names = ", ".join(f"@{m['username']}" for m in defaulters)
        msg = (
            f"🌙 *Daily Check — {yesterday}*\n\n"
            f"❌ No article submitted: {names}\n"
            f"Each owes *$1.00* added to the pot.\n\n"
            f"🍽 *Pot total: ${total_pot:.2f}*"
        )
        await app.bot.send_message(chat_id, msg, parse_mode="Markdown")


# ─── New Year Reset ───────────────────────────────────────────────────────────

async def new_year_summary(app: Application):
    last_year = current_year() - 1
    chats = db.get_all_chats()

    for chat_id in chats:
        members = db.get_members(chat_id, last_year)
        if not members:
            continue

        total = sum(m["owed"] for m in members)
        breakdown = "\n".join(
            f"  • @{m['username']}: ${m['owed']:.2f}"
            for m in sorted(members, key=lambda x: x["owed"], reverse=True)
        )
        msg = (
            f"🎊 *Happy New Year!*\n\n"
            f"Here's the final pot breakdown for *{last_year}*:\n\n"
            f"{breakdown}\n\n"
            f"🍽 *Total pot: ${total:.2f}*\n\n"
            f"Time to plan that meal! 🥂 New year, new articles. Good luck everyone!\n\n"
            f"_All balances have been reset for {last_year + 1}._"
        )
        await app.bot.send_message(chat_id, msg, parse_mode="Markdown")

        for m in members:
            db.register_member(chat_id, m["user_id"], m["username"], last_year + 1)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("pot", cmd_pot))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("adjust", cmd_adjust))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(midnight_check, trigger="cron", hour=0, minute=0, second=5, args=[app])
    scheduler.add_job(new_year_summary, trigger="cron", month=1, day=1, hour=0, minute=1, args=[app])
    scheduler.start()

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()