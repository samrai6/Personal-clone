import asyncio
import base64
import html
import logging
import os
import re
import random
import sqlite3
from pathlib import Path
from difflib import SequenceMatcher

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReactionTypeEmoji
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


# Match Ironman's random message reaction behavior.
REACTIONS = [
    "🤝", "😇", "🤗", "😍", "👍", "🎅", "😐", "🥰", "🤩", "😱",
    "🤣", "😘", "👏", "😛", "😈", "🎉", "⚡️", "🫡", "🤓", "😎",
    "🏆", "🔥", "🤭", "🌚", "🆒", "👻", "😁",
]


async def react_to_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    try:
        await context.bot.set_message_reaction(
            chat_id=message.chat_id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(random.choice(REACTIONS))],
            is_big=True,
        )
    except Exception as exc:
        log.debug("Message reaction skipped (%s)", type(exc).__name__)

TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = 1315244338
# TMDB Read Access Token from Ironman poster plugin.
TMDB_BEARER_TOKEN = base64.b64decode("ZXlKaGJHY2lPaUpJVXpJMU5pSjkuZXlKaGRXUWlPaUkyWkdVM1lUSXlaR1UxWWpFNVlURmpObVV5WkdVNVpXRXlNekUyWm1ReE1DSXNJbTVpWmlJNk1UYzBOVE15TWpRMk1pNDFNek1zSW5OMVlpSTZJalk0TURjNE1XUmxZelZqT0RBek5XWmlNRGhoTmpFeE5DSXNJbk5qYjNCbGN5STZXeUpoY0dsZmNtVmhaQ0pkTENKMlpYSnphVzl1SWpveGZRLnJNTUoyLVBCSXY4WTd5YnhQSUVwSWx6VEVYenV3cm05cnVLeEFVQ0FzYnc=").decode("utf-8")
MOVIE_BOT = "Samp_yu5bot"
TMDB_BASE = "https://api.themoviedb.org/3"
LANGUAGE = "en-US"

COUNTRY = "IN"

# Use PostgreSQL when DATABASE_URL is configured (recommended for Render).
# SQLite remains available for local testing, but its data is not durable on Render Free.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = Path(".")
DB_PATH = DATA_DIR / "personal_clone.db"


class DatabaseConnection:
    def __init__(self, connection, postgres=False):
        self.connection = connection
        self.postgres = postgres

    def execute(self, sql, params=()):
        if self.postgres:
            sql = sql.replace("?", "%s")
        return self.connection.execute(sql, params)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type:
                self.connection.rollback()
            else:
                self.connection.commit()
        finally:
            self.connection.close()


def db_connect():
    if DATABASE_URL:
        import psycopg
        database_url = DATABASE_URL
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://"):]
        raw = psycopg.connect(database_url)
        conn = DatabaseConnection(raw, postgres=True)
    else:
        conn = DatabaseConnection(sqlite3.connect(DB_PATH))

    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS relay_map (source_message_id BIGINT PRIMARY KEY, user_id BIGINT NOT NULL)")
    return conn


def get_source_channel():
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='source_channel_id'").fetchone()
    return int(row[0]) if row else None


def set_source_channel(chat_id):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('source_channel_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(chat_id),),
        )


def remove_source_channel():
    with db_connect() as conn:
        conn.execute("DELETE FROM settings WHERE key='source_channel_id'")


def save_relay_mapping(source_message_id, user_id):
    with db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO relay_map(source_message_id, user_id) VALUES(?, ?)",
            (source_message_id, user_id),
        )


def get_relay_user(source_message_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM relay_map WHERE source_message_id=?",
            (source_message_id,),
        ).fetchone()
    return int(row[0]) if row else None


def source_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Set Source" if not get_source_channel() else "🔄 Change Source", callback_data="fs:set")],
        [InlineKeyboardButton("🗑 Remove Source", callback_data="fs:remove")],
    ])


def auto_delete_delay(chat_type):
    return 600 if chat_type == "private" else 60


async def delete_later(bot, chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        log.info("Auto-delete skipped (%s)", type(exc).__name__)


def schedule_auto_delete(message, bot):
    delay = auto_delete_delay(message.chat.type)
    asyncio.create_task(delete_later(bot, message.chat_id, message.message_id, delay))


async def reply_text_auto(message, *args, **kwargs):
    sent = await message.reply_text(*args, **kwargs)
    schedule_auto_delete(sent, message.get_bot())
    return sent


async def reply_photo_auto(message, *args, **kwargs):
    sent = await message.reply_photo(*args, **kwargs)
    schedule_auto_delete(sent, message.get_bot())
    return sent


async def tmdb_get(path, params=None):
    query = dict(params or {})
    query.setdefault("language", LANGUAGE)
    headers = {"Authorization": f"Bearer {TMDB_BEARER_TOKEN}", "accept": "application/json"}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(TMDB_BASE + path, params=query, headers=headers)
        response.raise_for_status()
        return response.json()


def movie_link(title):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title.strip()).strip("-") or "movie"
    return f"https://t.me/{MOVIE_BOT}?start=" + ("getfile-" + slug)[:64].strip("-")


def title_similarity(query, title):
    """Compare titles while ignoring punctuation, spaces and common subtitles."""
    clean_query = re.sub(r"[^a-z0-9]+", "", query.casefold())
    clean_title = re.sub(r"[^a-z0-9]+", "", title.casefold())
    if not clean_query or not clean_title:
        return 0.0
    if clean_query == clean_title or clean_title.startswith(clean_query):
        return 1.0
    if clean_query in clean_title:
        return 0.95
    return SequenceMatcher(None, clean_query, clean_title).ratio()


async def find_title(query):
    """Search both movies and TV shows; prefer the strongest title match."""
    found = []
    for kind, endpoint in (("movie", "/search/movie"), ("tv", "/search/tv")):
        data = await tmdb_get(endpoint, {"query": query, "include_adult": "false", "page": 1})
        for item in data.get("results", [])[:10]:
            name = item.get("title") if kind == "movie" else item.get("name")
            if not name:
                continue
            ratio = title_similarity(query, name)
            found.append((ratio, kind, item))
    if not found:
        return None
    found.sort(key=lambda row: (row[0], float(row[2].get("popularity") or 0)), reverse=True)
    return found[0][1], found[0][2], found[0][0]


async def find_matching_titles(query):
    """Return distinct TMDB movie and TV title suggestions for group spelling help."""
    matches = []
    seen = set()
    for kind, endpoint in (("movie", "/search/movie"), ("tv", "/search/tv")):
        data = await tmdb_get(endpoint, {"query": query, "include_adult": "false", "page": 1})
        for item in data.get("results", [])[:10]:
            name = item.get("title") if kind == "movie" else item.get("name")
            if not name:
                continue
            key = re.sub(r"[^a-z0-9]+", "", name.casefold())
            clean_query = re.sub(r"[^a-z0-9]+", "", query.casefold())
            if key == clean_query or key in seen:
                continue
            seen.add(key)
            ratio = title_similarity(query, name)
            matches.append((ratio, float(item.get("popularity") or 0), name))
    matches.sort(key=lambda row: (row[0], row[1]), reverse=True)
    # Limit suggestions to the five closest matches.
    # Ignore weak matches so unrelated titles are never suggested.
    if not matches or matches[0][0] < 0.45:
        return []
    return [name for _, _, name in matches[:5]]


async def title_details(kind, item):
    item_id = item["id"]
    append = "credits,external_ids"
    details = await tmdb_get(f"/{kind}/{item_id}", {"append_to_response": append})
    title = details.get("title") if kind == "movie" else details.get("name")
    title = title or (item.get("title") if kind == "movie" else item.get("name")) or "Unknown"
    date = details.get("release_date") if kind == "movie" else details.get("first_air_date")
    year = date[:4] if date else "N/A"
    overview = details.get("overview") or "Synopsis not available."
    rating = details.get("vote_average")
    genres = ", ".join(g.get("name", "") for g in details.get("genres", []) if g.get("name")) or "N/A"
    cast = ", ".join(p.get("name", "") for p in details.get("credits", {}).get("cast", [])[:5] if p.get("name")) or "N/A"
    imdb_id = details.get("imdb_id") or details.get("external_ids", {}).get("imdb_id")
    return {
        "title": title, "year": year, "date": date or "Not announced",
        "poster": details.get("poster_path") or item.get("poster_path"),
        "overview": overview, "rating": f"{float(rating):.1f}/10 (TMDB)" if rating else "N/A",
        "genres": genres, "cast": cast, "imdb_id": imdb_id,
        "kind": "Movie" if kind == "movie" else "Web Series",
    }


async def release_and_ott(kind, item_id):
    """Return India-only theatrical/first-air and streaming availability."""
    release_lines, provider_lines = [], []
    india_ott_date = "Not Announced Yet"
    if kind == "movie":
        try:
            data = await tmdb_get(f"/movie/{item_id}/release_dates")
            india = next((row for row in data.get("results", []) if row.get("iso_3166_1") == "IN"), {})
            dates = india.get("release_dates", [])
            theatrical = sorted({x.get("release_date", "")[:10] for x in dates if x.get("type") == 3 and x.get("release_date")})
            digital = sorted({x.get("release_date", "")[:10] for x in dates if x.get("type") == 4 and x.get("release_date")})
            release_lines.append("🇮🇳 India Theatrical Release Date: " + (theatrical[0] if theatrical else "Not Announced Yet"))
            if digital:
                india_ott_date = digital[0]
        except Exception as exc:
            log.warning("India release-date lookup unavailable (%s)", type(exc).__name__)
            release_lines.append("🇮🇳 India Theatrical Release Date: Not Announced Yet")
    else:
        release_lines.append("🇮🇳 India First Air Date: Not Announced Yet")
    try:
        data = await tmdb_get(f"/{kind}/{item_id}/watch/providers")
        india = (data.get("results") or {}).get("IN") or {}
        names = []
        for group in ("flatrate", "free", "ads"):
            for entry in india.get(group, []):
                name = entry.get("provider_name")
                if name and name not in names:
                    names.append(name)
        provider_lines.append("🇮🇳 India OTT Platform: " + (", ".join(names) if names else "Not Announced Yet"))
        provider_lines.append("📌 ಟೆಲಿಗ್ರಾಂಗೆ ಬರುವ ಸಾಧ್ಯತೆ: " + india_ott_date)
        provider_lines.append("📅 India OTT Release Date: " + india_ott_date)
    except Exception as exc:
        log.warning("India OTT lookup unavailable (%s)", type(exc).__name__)
        provider_lines.append("🇮🇳 India OTT Platform: Not Announced Yet")
        provider_lines.append("📌 ಟೆಲಿಗ್ರಾಂಗೆ ಬರುವ ಸಾಧ್ಯತೆ: " + india_ott_date)
        provider_lines.append("📅 India OTT Release Date: " + india_ott_date)
    return release_lines, provider_lines

async def show_title_suggestions(message, query):
    matches = await find_matching_titles(query)
    if not matches:
        return False
    suggestions = "\n".join(
        f"{index}. <code>{html.escape(name)}</code>"
        for index, name in enumerate(matches, start=1)
    )
    await reply_text_auto(message, 
        "<b>ನೀವು ಹುಡುಕುತ್ತಿರುವ Movie / Web Series ಇದರಲ್ಲಿ ಇರಬಹುದೇ?\n"
        "ಹೊಂದಾಣಿಕೆಯ ಹೆಸರುಗಳು:</b>\n"
        f"{suggestions}\n\n"
        "<b>Did you mean one of these?</b>\n"
        f"{suggestions}",
        parse_mode="HTML",
        reply_to_message_id=message.message_id,
    )
    return True


async def show_movie(message, query, group_check=False):
    if group_check:
        await show_title_suggestions(message, query)
        return

    result = await find_title(query)
    if not result:
        return await show_title_suggestions(message, query)

    # In PM, show details only when the searched title exactly matches
    # the TMDB title (ignoring spaces and punctuation). Otherwise show
    # the same spelling suggestions used in groups.
    kind, item, similarity = result
    matched_title = item.get("title") if kind == "movie" else item.get("name")
    clean_query = re.sub(r"[^a-z0-9]+", "", query.casefold())
    clean_title = re.sub(r"[^a-z0-9]+", "", (matched_title or "").casefold())
    if not clean_query or clean_query != clean_title:
        return await show_title_suggestions(message, query)

    details = await title_details(kind, item)
    release_lines, provider_lines = await release_and_ott(kind, item["id"])
    lines = [
        f"🎬 <b>{html.escape(details['title'])} ({html.escape(details['year'])})</b>",
        f"📌 <b>Type:</b> {details['kind']}",
        f"⭐ <b>IMDb Rating:</b> Not Available",
        f"🎭 <b>Genre:</b> {html.escape(details['genres'])}",
        f"👥 <b>Cast:</b> {html.escape(details['cast'])}",
        "",
        "<b>🇮🇳 India Release Information:</b>",
        *[html.escape(x) for x in (release_lines or ["India release-date details not available."])],
        "",
        "<b>📺 India OTT Information:</b>",
        *[html.escape(x) for x in (provider_lines or ["🇮🇳 India OTT Platform: Not Announced Yet"])],
        "",
        html.escape(details["overview"]),
    ]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬇️ Download", url=movie_link(details["title"]))
    ]])
    caption = "<b>" + "\n".join(lines) + "</b>"
    if len(caption) > 3900:
        overview = details["overview"]
        fixed = "\n".join(lines[:-1]) + "\n"
        remaining = max(0, 3700 - len("<b>" + fixed + "</b>"))
        lines[-1] = html.escape(overview[:remaining].rstrip() + ("…" if len(overview) > remaining else ""))
        caption = "<b>" + "\n".join(lines) + "</b>"
    poster = details.get("poster")
    if poster and len(caption) <= 1024:
        await reply_photo_auto(message, photo=f"https://image.tmdb.org/t/p/w500{poster}", caption=caption, parse_mode="HTML", reply_markup=keyboard)
    elif poster:
        await reply_photo_auto(message, photo=f"https://image.tmdb.org/t/p/w500{poster}", reply_markup=keyboard)
        await reply_text_auto(message, caption, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await reply_text_auto(message, caption, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
    return True
    return True



async def fs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    channel_id = get_source_channel()
    status = f"Current Source Channel ID: {channel_id}" if channel_id else "Source Channel ಇನ್ನೂ set ಆಗಿಲ್ಲ."
    await reply_text_auto(
        update.effective_message,
        f"<b>Source Channel Settings</b>\\n{status}\\n\\nಕೆಳಗಿನ buttons ಬಳಸಿ:",
        parse_mode="HTML",
        reply_markup=source_settings_keyboard(),
    )


async def fs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        await query.answer("Owner ಮಾತ್ರ ಬಳಸಬಹುದು.", show_alert=True)
        return
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "set":
        context.user_data["waiting_for_source_channel"] = True
        await query.message.reply_text(
            "Source Channel‌ನ ಯಾವುದಾದರೂ ಒಂದು post ಅನ್ನು ಇಲ್ಲಿ Forward ಮಾಡಿ.\\n"
            "Bot ಆ Channel ಅನ್ನು Source ಆಗಿ save ಮಾಡುತ್ತದೆ."
        )
    elif action == "remove":
        remove_source_channel()
        context.user_data.pop("waiting_for_source_channel", None)
        await query.edit_message_text(
            "Source Channel ತೆಗೆದುಹಾಕಲಾಗಿದೆ.",
            reply_markup=source_settings_keyboard(),
        )


async def accept_source_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or update.effective_user.id != OWNER_ID:
        return
    if not context.user_data.get("waiting_for_source_channel"):
        return
    origin = message.forward_origin
    source_chat = getattr(origin, "chat", None) if origin else None
    if not source_chat or source_chat.type != "channel":
        await reply_text_auto(message, "ದಯವಿಟ್ಟು Channel‌ನ original post ಅನ್ನು Forward ಮಾಡಿ.")
        return
    set_source_channel(source_chat.id)
    context.user_data.pop("waiting_for_source_channel", None)
    await reply_text_auto(
        message,
        f"✅ Source Channel set ಆಯಿತು.\\nChannel ID: <code>{source_chat.id}</code>",
        parse_mode="HTML",
    )


async def relay_private_message(message, context: ContextTypes.DEFAULT_TYPE):
    source_id = get_source_channel()
    if not source_id:
        return False
    user = message.from_user
    if not user:
        return False
    name = user.full_name or "Unknown"
    username = f"@{user.username}" if user.username else "Not set"
    header = (
        "📩 New PM\\n"
        f"Name: {name}\\n"
        f"Username: {username}\\n"
        f"User ID: {user.id}\\n"
        "━━━━━━━━━━━━━━"
    )
    if message.text:
        sent = await context.bot.send_message(
            chat_id=source_id,
            text=(header + "\\n" + message.text)[:4000],
            disable_web_page_preview=True,
        )
        save_relay_mapping(sent.message_id, user.id)
    else:
        header_msg = await context.bot.send_message(chat_id=source_id, text=header)
        save_relay_mapping(header_msg.message_id, user.id)
        copied = await context.bot.copy_message(
            chat_id=source_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
        save_relay_mapping(copied.message_id, user.id)
    return True


def airbrother_text_link(title):
    """Build an AirbrotherBot compose link, separating uppercase acronyms."""
    words = re.findall(r"[A-Za-z0-9]+", title.strip())
    parts = []
    for word in words:
        if word.isalpha() and word.isupper() and len(word) > 1:
            parts.extend(list(word))
        else:
            parts.append(word.capitalize())
    slug = "-".join(parts) or "movie"
    return f"https://t.me/AirbrotherBot?text={slug}"


async def send_owner_reply_to_user(message, user_id, context):
    """Deliver owner shortcuts and links to the original user's PM."""
    text = message.text or message.caption
    shortcut = re.match(r"^\s*s\s*-\s*(.+?)\s*$", text or "", flags=re.IGNORECASE)
    if shortcut:
        title = shortcut.group(1)
        url = airbrother_text_link(title)
        sent = await context.bot.send_message(
            chat_id=user_id,
            text=f'<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>',
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        asyncio.create_task(delete_later(context.bot, user_id, sent.message_id, 600))
        return
    if text and text.strip().startswith("https://t.me/") and "?start=getfile-" in text:
        match = re.search(r"getfile-([A-Za-z0-9-]+)", text)
        if match:
            title = match.group(1).replace("-", " ")
            sent = await context.bot.send_message(
                chat_id=user_id,
                text=f'<a href="{html.escape(text.strip(), quote=True)}">{html.escape(title)}</a>',
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            asyncio.create_task(delete_later(context.bot, user_id, sent.message_id, 600))
            return
    copied = await context.bot.copy_message(
        chat_id=user_id,
        from_chat_id=message.chat_id,
        message_id=message.message_id,
    )
    asyncio.create_task(delete_later(context.bot, user_id, copied.message_id, 600))


async def source_channel_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message or message.chat_id != get_source_channel():
        return
    if not message.reply_to_message:
        return
    if message.from_user and message.from_user.id != OWNER_ID:
        return
    user_id = get_relay_user(message.reply_to_message.message_id)
    if not user_id:
        return
    try:
        await send_owner_reply_to_user(message, user_id, context)
    except Exception as exc:
        log.warning("Could not deliver source-channel reply (%s)", type(exc).__name__)


async def source_discussion_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    source_id = get_source_channel()
    if not message or not source_id or not message.reply_to_message:
        return
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    original = message.reply_to_message
    origin = original.forward_origin
    origin_chat = getattr(origin, "chat", None) if origin else None
    origin_message_id = getattr(origin, "message_id", None) if origin_chat and origin_chat.id == source_id else None
    if not origin_message_id:
        return
    user_id = get_relay_user(origin_message_id)
    if not user_id:
        return
    try:
        await send_owner_reply_to_user(message, user_id, context)
    except Exception as exc:
        log.warning("Discussion reply delivery failed (%s)", type(exc).__name__)


async def private_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or update.effective_user.id != message.from_user.id:
        return
    try:
        sent = await relay_private_message(message, context)
        if not sent:
            await reply_text_auto(message, "Source Channel set ಮಾಡಲು /fs ಬಳಸಿ.")
    except Exception:
        log.exception("Private message relay failed")
        await reply_text_auto(message, "Message Source Channel‌ಗೆ ಕಳುಹಿಸಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    payload = context.args[0] if context.args else ""
    if payload.startswith("getfile-"):
        title = payload[len("getfile-"):].strip()
        try:
            await message.delete()
        except Exception:
            pass
        if title:
            await context.bot.send_message(chat_id=message.chat_id, text=title)
        return
    await reply_text_auto(
        message,
        "ನಮಸ್ಕಾರ! Movie ಅಥವಾ Web Series ಹೆಸರು ಕಳುಹಿಸಿ. TMDB details ಕೊಡುತ್ತೇನೆ."
    )


async def private_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return
    query = message.text.strip()
    if len(query) < 2 or len(query) > 100:
        return
    try:
        handled = await show_movie(message, query)
        if not handled:
            sent = await relay_private_message(message, context)
            if not sent:
                await reply_text_auto(message, "ಹತ್ತಿರದ Movie / Web Series ಹೆಸರು ಸಿಗಲಿಲ್ಲ.\\nNo close matching titles found.")
    except httpx.HTTPStatusError as exc:
        log.warning("TMDB request failed with HTTP %s", exc.response.status_code)
        await reply_text_auto(message, "TMDB search ಈಗ ಲಭ್ಯವಿಲ್ಲ. ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಪ್ರಯತ್ನಿಸಿ.")
    except Exception as exc:
        log.exception("Movie lookup failed")
        await reply_text_auto(message, "ಕ್ಷಮಿಸಿ, Movie details ಹುಡುಕಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ.")


async def group_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return
    query = message.text.strip()
    if len(query) < 3 or len(query) > 90 or len(query.split()) > 8 or query.startswith("/"):
        return
    try:
        await show_movie(message, query, group_check=True)
    except Exception as exc:
        log.exception("Group TMDB title check failed")


async def health_check(reader, writer):
    try:
        await reader.readuntil(b"\\r\\n\\r\\n")
        body = b"Personal-clone Telegram bot is running"
        writer.write(
            b"HTTP/1.1 200 OK\\r\\n"
            b"Content-Type: text/plain; charset=utf-8\\r\\n"
            + f"Content-Length: {len(body)}\\r\\n".encode()
            + b"Connection: close\\r\\n\\r\\n"
            + body
        )
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def start_health_server(app):
    port = int(os.getenv("PORT", "10000"))
    server = await asyncio.start_server(health_check, "0.0.0.0", port)
    app.bot_data["health_server"] = server
    app.bot_data["health_server_task"] = asyncio.create_task(server.serve_forever())
    log.info("Health server listening on 0.0.0.0:%s", port)


async def startup_notify(app):
    await start_health_server(app)
    try:
        sent = await app.bot.send_message(chat_id=OWNER_ID, text="I'm.alive sir 🤓")
        schedule_auto_delete(sent, app.bot)
        log.info("Startup notification sent to owner")
    except Exception as exc:
        log.warning("Startup notification failed (%s)", type(exc).__name__)


def main():
    missing = []
    if not TOKEN:
        missing.append("BOT_TOKEN")
    if missing:
        raise SystemExit("Missing configuration: " + ", ".join(missing))
    app = ApplicationBuilder().token(TOKEN).post_init(startup_notify).build()
    # React to incoming PM and group messages without interrupting existing handlers.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, react_to_message), group=-2)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, react_to_message), group=-2)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fs", fs_command))
    app.add_handler(CallbackQueryHandler(fs_callback, pattern=r"^fs:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.FORWARDED, accept_source_forward), group=-1)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_reply))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.TEXT & ~filters.COMMAND, private_non_text))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.REPLY, source_discussion_reply), group=-1)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_check))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, source_channel_reply))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
