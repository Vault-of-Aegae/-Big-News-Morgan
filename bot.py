"""
News Article Accountability Bot
- Tracks who sends news articles daily in a Telegram group
- Uses Google Gemini AI (free) to verify if a link is a legitimate news article
- At midnight (SGT), announces who defaulted and adds $1 to their tab
- Resets yearly
"""

import os
import logging
import asyncio
from datetime import datetime, time
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


# ─── Helpers ─────────────────────────────────────────────────────────────────

def today_str() -> str:
    """Return today's date string in SGT."""
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def current_year() -> int:
    return datetime.now(TIMEZONE).year


async def is_news_article(url: str) -> tuple[bool, str, str]:
    """
    Use Google Gemini (free) to determine if a URL is a legitimate news article.
    Returns (is_valid, reason, summary).
    Summary is a 2-3 sentence plain-English overview of the article (empty string if invalid).
    """
    try:
        # First try to fetch page content as extra context
        page_snippet = ""
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200:
                    page_snippet = resp.text[:3000]
        except Exception:
            pass  # Will still work using just the URL

        prompt = f"""You are a fact-checker determining if a URL points to a legitimate news article.

URL: {url}

{"Page HTML snippet (first 3000 chars):" if page_snippet else "Note: Could not fetch page content."}
{page_snippet[:3000] if page_snippet else ""}

Determine if this is a REAL news article (not a blog post, social media post, video, forum thread, or spam).

A legitimate news article:
- Comes from a recognizable news outlet (local or international)
- Reports on real events, current affairs, politics, business, sports, science, etc.
- Has a byline, date, or publication info (or the domain strongly implies it)

Respond in this exact format with no extra text:
VALID: true/false
REASON: one sentence explanation
SUMMARY: 2-3 sentence plain English summary of what the article is about (leave blank if not a valid article)"""

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                GEMINI_URL,
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}]},
            )
            data = response.json()

        response_text = (
            data["candidates"][0]["content"]["parts"][0]["text"].strip()
        )
        lines = response_text.splitlines()

        is_valid = False
        reason = "Could not determine."
        summary = ""

        for line in lines:
            if line.startswith("VALID:"):
                is_valid = "true" in line.lower()
            elif line.startswith("REASON:"):
                reason = line.replace("REASON:", "").strip()
            elif line.startswith("SUMMARY:"):
                summary = line.replace("SUMMARY:", "").strip()

        return is_valid, reason, summary

    except Exception as e:
        logger.error(f"Error checking article: {e}")
        return False, "Could not verify the link.", ""


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
    for r in records[-20:]:  # Last 20 entries
        status = "✅" if r["submitted"] else "❌ ($1)"
        msg += f"{status} {r['date']}\n"

    owed = db.get_member_owed(chat_id, user.id, year)
    msg += f"\n💸 Total owed: *${owed:.2f}*"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manually adjust a member's owed amount.

    Usage:
      /adjust @username set 5       → set their balance to exactly $5.00
      /adjust @username add 2       → add $2.00 to their balance
      /adjust @username remove 1    → subtract $1.00 from their balance
    """
    chat_id = update.effective_chat.id
    year = current_year()
    args = context.args  # list of words after /adjust

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

    # Normalise @username
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

    # Find the member by username (case-insensitive)
    members = db.get_members(chat_id, year)
    target = next(
        (m for m in members if m["username"].lower() == target_username), None
    )

    if not target:
        await update.message.reply_text(
            f"❌ Couldn't find `@{target_username}` in this group's registered members.\n"
            f"Make sure they've used `/register` and that you spelled the username correctly.",
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
    else:  # remove
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
    """Detect URLs in messages and verify if they're news articles."""
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text
    today = today_str()
    year = current_year()

    # Only track registered members
    if not db.is_member(chat_id, user.id, year):
        return

    # Extract URLs
    urls = []
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type in ("url", "text_link"):
                if entity.type == "url":
                    urls.append(text[entity.offset : entity.offset + entity.length])
                else:
                    urls.append(entity.url)

    # Also naive check for http in text
    if not urls:
        words = text.split()
        urls = [w for w in words if w.startswith("http://") or w.startswith("https://")]

    if not urls:
        return

    # Check if already submitted today
    if db.has_submitted_today(chat_id, user.id, today):
        await update.message.reply_text(
            f"✅ @{user.username or user.first_name}, you've already submitted your article today! 🎉",
            reply_to_message_id=update.message.message_id,
        )
        return

    # Verify the first URL found
    url = urls[0]
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
    """Run at midnight SGT: find defaulters, add $1, announce in group."""
    yesterday = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    # Note: this runs at the START of the new day, so "yesterday" is the day just ended
    from datetime import timedelta
    yesterday_dt = datetime.now(TIMEZONE) - timedelta(days=1)
    yesterday = yesterday_dt.strftime("%Y-%m-%d")

    logger.info(f"Running midnight check for {yesterday}")

    # Get all registered chats
    chats = db.get_all_chats()
    year = current_year()

    for chat_id in chats:
        members = db.get_members(chat_id, year)
        submitted_ids = {s["user_id"] for s in db.get_submissions_today(chat_id, yesterday)}

        defaulters = [m for m in members if m["user_id"] not in submitted_ids]

        if not defaulters:
            await app.bot.send_message(
                chat_id,
                f"🎉 Everyone submitted a news article yesterday! No one owes anything. Keep it up! 📰",
            )
            continue

        # Add $1 to each defaulter
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
    """Run on Jan 1: announce final pot and reset for new year."""
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

        # Register everyone for the new year automatically
        for m in members:
            db.register_member(chat_id, m["user_id"], m["username"], last_year + 1)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register handlers
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

    # Scheduler
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # Midnight daily check
    scheduler.add_job(
        midnight_check,
        trigger="cron",
        hour=0,
        minute=0,
        second=5,
        args=[app],
    )

    # New Year summary (Jan 1 at 00:01)
    scheduler.add_job(
        new_year_summary,
        trigger="cron",
        month=1,
        day=1,
        hour=0,
        minute=1,
        args=[app],
    )

    scheduler.start()

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
