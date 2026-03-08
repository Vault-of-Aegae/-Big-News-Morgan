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
import asyncio
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

# ─── Trusted news domains — always accepted, no AI check needed ───────────────
TRUSTED_NEWS_DOMAINS = {
    # ── Singapore ────────────────────────────────────────────────────────────
    "straitstimes.com", "channelnewsasia.com", "cna.asia", "todayonline.com",
    "mothership.sg", "zaobao.com", "beritaharian.sg", "tamilmurasu.com.sg",
    "businesstimes.com.sg", "theedgesingapore.com", "asiaone.com",
    "tnp.sg", "newpaper.com.sg", "ricemedia.co", "theonlinecitizen.com",
    "dollars-and-sense.sg", "sgcarmart.com.sg",

    # ── Southeast Asia ───────────────────────────────────────────────────────
    # Malaysia
    "thestar.com.my", "nst.com.my", "freemalaysiatoday.com", "malaymail.com",
    "malaysiakini.com", "sinchew.com.my", "theedgemarkets.com",
    "bernama.com", "astroawani.com",
    # Indonesia
    "kompas.com", "thejakartapost.com", "detik.com", "tempo.co",
    "antaranews.com", "tribunnews.com", "republika.co.id",
    # Philippines
    "rappler.com", "philstar.com", "inquirer.net", "abs-cbn.com",
    "gmanetwork.com", "mb.com.ph", "pna.gov.ph",
    # Thailand
    "bangkokpost.com", "nationthailand.com", "thaipbs.or.th",
    # Vietnam
    "vietnamnews.vn", "tuoitrenews.vn", "vnexpress.net",
    # Myanmar
    "irrawaddy.com", "mizzima.com",
    # Cambodia / Laos
    "phnompenhpost.com", "khmertimeskh.com",

    # ── Asia Pacific ─────────────────────────────────────────────────────────
    # Hong Kong / China
    "scmp.com", "hk01.com", "rthk.hk", "chinadaily.com.cn",
    "globaltimes.cn", "xinhuanet.com",
    # Japan
    "japantimes.co.jp", "nhk.or.jp", "asahi.com", "mainichi.jp",
    "yomiuri.co.jp", "nikkei.com",
    # South Korea
    "koreatimes.co.kr", "koreaherald.com", "yonhapnewsagency.com",
    # Taiwan
    "taipeitimes.com", "focustaiwan.tw",
    # India
    "thehindu.com", "hindustantimes.com", "ndtv.com", "timesofindia.com",
    "indianexpress.com", "theprint.in", "scroll.in", "thewire.in",
    "livemint.com", "economictimes.com", "businessstandard.com",
    "news18.com", "firstpost.com", "india.com", "dnaindia.com",
    # Australia / New Zealand
    "abc.net.au", "smh.com.au", "theage.com.au", "afr.com",
    "theaustralian.com.au", "news.com.au", "heraldsun.com.au",
    "stuff.co.nz", "nzherald.co.nz", "rnz.co.nz",
    # Pakistan / Bangladesh / Sri Lanka
    "dawn.com", "geo.tv", "thenews.com.pk", "thedailystar.net",
    "colombopage.com", "dailymirror.lk",

    # ── Middle East ──────────────────────────────────────────────────────────
    "aljazeera.com", "arabnews.com", "gulfnews.com", "thenationalnews.com",
    "khaleejtimes.com", "haaretz.com", "timesofisrael.com", "jpost.com",
    "dailysabah.com", "hurriyet.com.tr", "tehrantimes.com",

    # ── Africa ───────────────────────────────────────────────────────────────
    "news24.com", "dailymaverick.co.za", "timeslive.co.za",
    "businessday.ng", "punchng.com", "vanguardngr.com", "theguardian.ng",
    "standardmedia.co.ke", "nation.africa", "monitor.co.ug",
    "mg.co.za", "iol.co.za",

    # ── UK ───────────────────────────────────────────────────────────────────
    "bbc.com", "bbc.co.uk", "theguardian.com", "independent.co.uk",
    "telegraph.co.uk", "thetimes.co.uk", "ft.com", "economist.com",
    "dailymail.co.uk", "mirror.co.uk", "thesun.co.uk", "express.co.uk",
    "metro.co.uk", "eveningstandard.co.uk", "cityam.com",
    "sky.com", "itv.com", "channel4.com",

    # ── Europe ───────────────────────────────────────────────────────────────
    "dw.com", "france24.com", "rfi.fr", "euronews.com", "politico.eu",
    "lemonde.fr", "lefigaro.fr", "liberation.fr",
    "spiegel.de", "faz.net", "sueddeutsche.de", "zeit.de",
    "corriere.it", "repubblica.it", "elpais.com", "elmundo.es",
    "rtve.es", "svt.se", "dn.se", "nrc.nl", "volkskrant.nl",
    "derstandard.at", "nzz.ch", "rts.ch", "yle.fi",
    "reuters.com",

    # ── US — General News ────────────────────────────────────────────────────
    "apnews.com", "nytimes.com", "washingtonpost.com", "wsj.com",
    "usatoday.com", "latimes.com", "nypost.com", "chicagotribune.com",
    "bostonglobe.com", "sfchronicle.com", "seattletimes.com",
    "cnn.com", "foxnews.com", "msnbc.com", "nbcnews.com",
    "abcnews.go.com", "cbsnews.com", "npr.org", "pbs.org",
    "theatlantic.com", "newyorker.com", "politico.com", "axios.com",
    "thehill.com", "salon.com", "slate.com", "vox.com",
    "huffpost.com", "buzzfeednews.com", "propublica.org",
    "motherjones.com", "thenation.com", "reason.com",
    "newsweek.com", "time.com", "rollingstone.com",

    # ── US — Business & Finance ──────────────────────────────────────────────
    "bloomberg.com", "cnbc.com", "forbes.com", "fortune.com",
    "businessinsider.com", "marketwatch.com", "barrons.com",
    "investopedia.com", "seekingalpha.com", "morningstar.com",

    # ── US — Tech ────────────────────────────────────────────────────────────
    "techcrunch.com", "theverge.com", "wired.com", "arstechnica.com",
    "engadget.com", "cnet.com", "zdnet.com", "venturebeat.com",
    "gizmodo.com", "mashable.com", "technologyreview.com",
    "9to5mac.com", "9to5google.com", "androidpolice.com",

    # ── Science & Health ─────────────────────────────────────────────────────
    "nature.com", "science.org", "newscientist.com", "scientificamerican.com",
    "statnews.com", "medscape.com", "webmd.com", "healthline.com",
    "livescience.com", "space.com", "nationalgeographic.com",
    "discovermagazine.com", "popularmechanics.com", "popsci.com",

    # ── Sport ────────────────────────────────────────────────────────────────
    "espn.com", "sportingnews.com", "goal.com", "skysports.com",
    "bbc.com/sport", "theathletic.com", "bleacherreport.com",
    "nfl.com", "nba.com", "mlb.com", "nhl.com",
    "fifa.com", "uefa.com", "olympics.com",

    # ── Canada ───────────────────────────────────────────────────────────────
    "cbc.ca", "globeandmail.com", "nationalpost.com", "torontostar.com",
    "vancouversun.com", "montrealgazette.com", "ctvnews.ca", "globalnews.ca",

    # ── Wire / Agencies ──────────────────────────────────────────────────────
    "reuters.com", "apnews.com", "afp.com", "bloomberg.com",
    "upi.com", "prnewswire.com", "businesswire.com",
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,<b>/</b>;q=0.8",
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
            title_match = re.search(r'<title[^>]<b>>(.</b>?)</title>', html, re.IGNORECASE | re.DOTALL)
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
                r'<(?:article|main)[^>]<b>>(.</b>?)</(?:article|main)>',
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

        # Quick accept: trusted news domains — no AI needed
        final_domain = get_domain(final_url)
        original_domain = get_domain(url)
        for domain in (final_domain, original_domain):
            if any(domain == td or domain.endswith("." + td) for td in TRUSTED_NEWS_DOMAINS):
                # Still generate a summary if we have page content
                summary = ""
                if page_content:
                    try:
                        summary_prompt = f"""Summarise this news article in 2-3 sentences in plain English.
Page content:
{page_content[:3000]}
Respond with ONLY the summary, nothing else."""
                        async with httpx.AsyncClient(timeout=15) as client:
                            resp = await client.post(
                                GEMINI_URL,
                                headers={"Content-Type": "application/json"},
                                json={"contents": [{"parts": [{"text": summary_prompt}]}]},
                            )
                            summary = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    except Exception:
                        summary = ""
                logger.info(f"Trusted domain auto-accepted: {domain}")
                return True, f"Recognised news outlet: {domain}.", summary

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

Be LENIENT. When in doubt, accept it.

ACCEPT if ANY of these are true:
- Domain looks like a news outlet (even if you don't recognise it — local/regional news sites count)
- URL structure contains words like /news/, /article/, /world/, /politics/, /business/, /sport/, /tech/
- Content reports on real events: current affairs, politics, business, sports, science, health, etc.
- Paywalled articles from any news-looking site — accept them
- Article has a headline, date, or byline

REJECT only if clearly NOT news:
- Obviously a blog, forum post, social media, product page, or spam
- Domain is entertainment/shopping/social (but these are already filtered before reaching you)
- Content is completely unrelated to any real-world event or topic

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
        summary_lines = []
        in_summary = False

        for line in response_text.splitlines():
            line_stripped = line.strip()
            if line_stripped.upper().startswith("VALID:"):
                in_summary = False
                is_valid = "true" in line_stripped.lower()
            elif line_stripped.upper().startswith("REASON:"):
                in_summary = False
                reason = line_stripped.split(":", 1)[1].strip()
            elif line_stripped.upper().startswith("SUMMARY:"):
                in_summary = True
                first_line = line_stripped.split(":", 1)[1].strip()
                if first_line:
                    summary_lines.append(first_line)
            elif in_summary and line_stripped:
                summary_lines.append(line_stripped)

        raw_summary = " ".join(summary_lines).strip()
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
        "👋 <b>News Accountability Bot</b>\n\n"
        "I track who sends a news article every day.\n"
        "If you miss a day, <b>$1 goes into the pot!</b> 💰\n\n"
        "Commands:\n"
        "/register — Join the accountability group\n"
        "/status — See today's submissions\n"
        "/leaderboard — See who owes the most\n"
        "/pot — Check the current pot total\n"
        "/history — Your personal submission history\n"
        "/adjust — Manually adjust someone's balance\n"
        "/help — Show this message",
        parse_mode="HTML",
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

    msg = f"📋 <b>Article Status for {today}</b>\n\n"
    if done:
        msg += "✅ <b>Submitted:</b>\n"
        for m in done:
            msg += f"  • @{m['username']}\n"
    if pending:
        msg += "\n⏳ <b>Still waiting:</b>\n"
        for m in pending:
            msg += f"  • @{m['username']}\n"
    if not members:
        msg += "<i>No members registered yet. Use /register to join!</i>"

    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    year = current_year()
    members = db.get_members(chat_id, year)

    if not members:
        await update.message.reply_text("No members registered yet!")
        return

    msg = f"💸 <b>Pot Contributions — {year}</b>\n\n"
    members_sorted = sorted(members, key=lambda m: m["owed"], reverse=True)
    for i, m in enumerate(members_sorted):
        emoji = "🥇" if i == 0 and m["owed"] > 0 else "👤"
        msg += f"{emoji} @{m['username']} — <b>${m['owed']:.2f}</b>\n"

    total = sum(m["owed"] for m in members)
    msg += f"\n🍽 <b>Total pot: ${total:.2f}</b>"
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_pot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    members = db.get_members(chat_id, current_year())
    total = sum(m["owed"] for m in members)
    await update.message.reply_text(
        f"🍽 Current pot: <b>${total:.2f}</b>\n"
        f"Keep sharing those articles to keep your tab clean! 📰",
        parse_mode="HTML",
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    year = current_year()
    records = db.get_user_history(chat_id, user.id, year)

    if not records:
        await update.message.reply_text("No submission history found for this year.")
        return

    msg = f"📅 <b>Your submission history ({year}):</b>\n\n"
    for r in records[-20:]:
        if r["submitted"]:
            url_part = f'\n    <a href="{r["url"]}">{r["url"][:50]}{"..." if len(r["url"]) > 50 else ""}</a>' if r.get("url") else ""
            msg += f"✅ {r['date']}{url_part}\n\n"
        else:
            msg += f"❌ {r['date']} ($1 owed)\n\n"

    owed = db.get_member_owed(chat_id, user.id, year)
    msg += f"\n💸 Total owed: <b>${owed:.2f}</b>"
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    year = current_year()
    args = context.args

    usage = (
        "⚙️ <b>Usage:</b>\n"
        "<code>/adjust @username set <amount></code> — set balance to exact amount\n"
        "<code>/adjust @username add <amount></code> — add to balance\n"
        "<code>/adjust @username remove <amount></code> — subtract from balance\n\n"
        "<i>Example: <code>/adjust @john remove 1</code></i>"
    )

    if not args or len(args) < 3:
        await update.message.reply_text(usage, parse_mode="HTML")
        return

    raw_target, action, raw_amount = args[0], args[1].lower(), args[2]
    target_username = raw_target.lstrip("@").lower()

    if action not in ("set", "add", "remove"):
        await update.message.reply_text(
            f"❌ Unknown action <code>{action}</code>. Use <code>set</code>, <code>add</code>, or <code>remove</code>.\n\n{usage}",
            parse_mode="HTML",
        )
        return

    try:
        amount = float(raw_amount)
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Amount must be a positive number, e.g. <code>1</code>, <code>2.50</code>.",
            parse_mode="HTML",
        )
        return

    members = db.get_members(chat_id, year)
    target = next(
        (m for m in members if m["username"].lower() == target_username), None
    )

    if not target:
        await update.message.reply_text(
            f"❌ Couldn't find <code>@{target_username}</code> in registered members.\n"
            f"Make sure they've used <code>/register</code> and the username is spelled correctly.",
            parse_mode="HTML",
        )
        return

    old_balance = target["owed"]

    if action == "set":
        db.set_owed(chat_id, target["user_id"], amount, year)
        new_balance = amount
        verb = f"set to <b>${new_balance:.2f}</b>"
    elif action == "add":
        db.add_owed(chat_id, target["user_id"], amount, year)
        new_balance = old_balance + amount
        verb = f"increased by <b>${amount:.2f}</b> → now <b>${new_balance:.2f}</b>"
    else:
        new_amount = max(0.0, old_balance - amount)
        db.set_owed(chat_id, target["user_id"], new_amount, year)
        new_balance = new_amount
        actually_removed = old_balance - new_amount
        verb = f"reduced by <b>${actually_removed:.2f}</b> → now <b>${new_balance:.2f}</b>"

    adjuster = update.effective_user.username or update.effective_user.first_name
    total_pot = sum(m["owed"] for m in db.get_members(chat_id, year))

    await update.message.reply_text(
        f"✏️ <b>Balance adjusted!</b>\n\n"
        f"👤 @{target['username']}'s balance {verb}\n"
        f"_(was ${old_balance:.2f})_\n\n"
        f"🍽 <b>New pot total: ${total_pot:.2f}</b>\n\n"
        f"_Adjusted by @{adjuster}_",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ─── Message Handler ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detect URLs in messages and verify if they are news articles."""
    logger.info(f"handle_message triggered — update_id={update.update_id}")

    if not update.message:
        logger.info("Ignored: no message object")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    today = today_str()
    year = current_year()

    logger.info(f"Message from user_id={user.id} @{user.username} in chat_id={chat_id}")
    logger.info(f"Message text: {update.message.text!r}")
    logger.info(f"Message entities: {update.message.entities}")

    is_member = db.is_member(chat_id, user.id, year)
    logger.info(f"Is registered member: {is_member}")

    if not is_member:
        logger.info(f"Ignored: user {user.id} not a registered member")
        return

    all_urls = extract_urls(update)
    logger.info(f"Extracted URLs: {all_urls}")

    url = pick_best_url(all_urls)
    logger.info(f"Best URL picked: {url}")

    if not url:
        logger.info("Ignored: no valid URL found in message")
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

    try:
        is_valid, reason, summary = await asyncio.wait_for(is_news_article(url), timeout=30)
    except asyncio.TimeoutError:
        await thinking_msg.edit_text(
            "⏱ Took too long to check that link — the site may be slow or blocking bots.\n\n"
            "Try sending the original article URL directly instead of a shortened link!"
        )
        return

    if is_valid:
        db.record_submission(chat_id, user.id, today, url, year)
        msg = (
            f"✅ <b>Article accepted!</b> @{user.username or user.first_name} has submitted their article for today.\n\n"
            + (f"📝 <b>Summary:</b> {summary}\n\n" if summary else "")
            + f"<i>{reason}</i>"
        )
        await thinking_msg.edit_text(msg, parse_mode="HTML")
    else:
        await thinking_msg.edit_text(
            f"❌ <b>Not accepted.</b> That doesn't appear to be a legitimate news article.\n\n"
            f"_{reason}_\n\n"
            f"Please send a valid news article link to fulfil today's quota!",
            parse_mode="HTML",
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
            f"🌙 <b>Daily Check — {yesterday}</b>\n\n"
            f"❌ No article submitted: {names}\n"
            f"Each owes <b>$1.00</b> added to the pot.\n\n"
            f"🍽 <b>Pot total: ${total_pot:.2f}</b>"
        )
        await app.bot.send_message(chat_id, msg, parse_mode="HTML")


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
            f"🎊 <b>Happy New Year!</b>\n\n"
            f"Here's the final pot breakdown for <b>{last_year}</b>:\n\n"
            f"{breakdown}\n\n"
            f"🍽 <b>Total pot: ${total:.2f}</b>\n\n"
            f"Time to plan that meal! 🥂 New year, new articles. Good luck everyone!\n\n"
            f"_All balances have been reset for {last_year + 1}._"
        )
        await app.bot.send_message(chat_id, msg, parse_mode="HTML")

        for m in members:
            db.register_member(chat_id, m["user_id"], m["username"], last_year + 1)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # DEBUG: catch every single update and log it
    async def debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"DEBUG RAW UPDATE: {update}")

    from telegram.ext import TypeHandler
    app.add_handler(TypeHandler(Update, debug_all), group=-1)

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