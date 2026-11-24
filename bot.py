import asyncio
from datetime import datetime
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
MOVIE_DATABASE_CHANNEL_ID = -1004402999657
DELETE_CHANNEL_ID = -1004392593374
# TMDB Read Access Token from Ironman poster plugin.
TMDB_BEARER_TOKEN = os.getenv("TMDB_BEARER_TOKEN", "").strip()
MOVIE_BOT = "Samp_yu5bot"
TMDB_BASE = "https://api.themoviedb.org/3"
OMDB_API_KEY = os.getenv("OMDB_API_KEY", "").strip()
OMDB_BASE = "https://www.omdbapi.com/"
LANGUAGE = "en-US"

COUNTRY = "IN"


def format_tmdb_date(value):
    """Format TMDb ISO dates as DD-MM-YYYY."""
    if not value:
        return "Not Announced Yet"
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except (TypeError, ValueError):
        return value

# Use PostgreSQL when DATABASE_URL is configured (recommended for Render).
# SQLite remains available for local testing, but its data is not durable on Render Free.
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_NAME = "personal_clone"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = Path(".")
DB_PATH = DATA_DIR / "personal_clone.db"


class MongoCursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        if not self.row:
            return None
        if "value" in self.row:
            return (self.row["value"],)
        if "user_id" in self.row:
            return (self.row["user_id"],)
        return None


class MongoDatabaseConnection:
    """Small adapter retaining the existing SQL-style calls over MongoDB."""
    def __init__(self):
        from pymongo import MongoClient
        self.client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        self.db = self.client[MONGODB_NAME]
        # Fail fast if the URI is invalid or MongoDB is unreachable.
        self.client.admin.command("ping")
        self.settings = self.db["settings"]
        self.relay_map = self.db["relay_map"]
        self.movies = self.db["movie_posts"]
        self.movies.create_index("title_base")

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("create table"):
            return MongoCursor()
        if normalized.startswith("select value from settings"):
            # Support both parameterized queries and SQL with a literal key.
            key = params[0] if params else None
            if key is None:
                match = re.search(r"\bkey\s*=\s*'([^']*)'", sql, re.IGNORECASE)
                if match:
                    key = match.group(1)
            if key is None:
                raise ValueError("Settings SELECT query is missing its key")
            return MongoCursor(self.settings.find_one({"key": str(key)}))
        if normalized.startswith("insert into settings"):
            self.settings.update_one(
                {"key": params[0]},
                {"$set": {"value": str(params[1])}},
                upsert=True,
            )
            return MongoCursor()
        if normalized.startswith("delete from settings"):
            self.settings.delete_one({"key": params[0]})
            return MongoCursor()
        if normalized.startswith("insert or replace into relay_map"):
            self.relay_map.replace_one(
                {"source_message_id": int(params[0])},
                {"source_message_id": int(params[0]), "user_id": int(params[1])},
                upsert=True,
            )
            return MongoCursor()
        if normalized.startswith("select user_id from relay_map"):
            return MongoCursor(self.relay_map.find_one({"source_message_id": int(params[0])}))
        raise NotImplementedError("Unsupported database query")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.client.close()


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
    if MONGODB_URI:
        return MongoDatabaseConnection()
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


def get_database_channel():
    return MOVIE_DATABASE_CHANNEL_ID


def set_database_channel(chat_id):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('movie_database_channel_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(chat_id),),
        )


def normalize_movie_key(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def extract_ott_info(caption):
    """Read India OTT platform and release date manually supplied in a database post."""
    text = re.sub(r"<[^>]+>", "", caption or "")
    platform = None
    release_date = None
    for line in text.splitlines():
        line = line.strip()
        platform_match = re.match(r"^India\s+OTT\s+Platform\s*:\s*(.+)$", line, re.IGNORECASE)
        date_match = re.match(r"^(?:India\s+OTT\s+(?:Release\s+)?Date|Date)\s*:\s*(.+)$", line, re.IGNORECASE)
        # Also accept compact title lines such as:
        # Lovely Runner (2024) – India OTT Date: 08-09-2025
        # Lovely Runner (2024) Date: 08-09-2025
        if not date_match:
            date_match = re.search(r"(?:[–—-]\s*India\s+OTT\s+|\b)Date\s*:\s*(.+)$", line, re.IGNORECASE)
        if platform_match:
            value = platform_match.group(1).strip()
            if value and value.casefold() not in {"not announced yet", "not available", "n/a"}:
                platform = value
        if date_match:
            value = date_match.group(1).strip()
            if value and value.casefold() not in {"not announced yet", "not available", "n/a", "ಬಂದಿಲ್ಲ"}:
                release_date = value
    return platform, release_date


def apply_saved_ott_info(provider_lines, saved):
    """Prefer provider OTT details; use only the saved MongoDB OTT date as a final fallback."""
    lines = list(provider_lines or [])
    saved = saved or {}

    def is_missing(value):
        return not value or str(value).strip().casefold() in {
            "not announced yet",
            "not available",
            "not available from imdb",
            "n/a",
            "not announced",
            "ಬಂದಿಲ್ಲ",
        }

    current_date = next(
        (line.split(":", 1)[1].strip() for line in lines if line.startswith("India OTT Release Date:")),
        "",
    )
    # Use the database date only when TMDB/IMDb has no OTT release date.
    fallback_date = ""
    if is_missing(current_date) and not is_missing(saved.get("ott_release_date")):
        fallback_date = str(saved["ott_release_date"]).strip()

    output = []
    date_line_added = False
    for line in lines:
        if line.startswith("India OTT Release Date:"):
            if not date_line_added and not fallback_date:
                output.append(line)
            date_line_added = True
        elif line.startswith("📌 ಟೆಲಿಗ್ರಾಂಗೆ ಬರುವ ಸಾಧ್ಯತೆ:"):
            value = fallback_date or (current_date if not is_missing(current_date) else "ಬಂದಿಲ್ಲ")
            output.append("📌 ಟೆಲಿಗ್ರಾಂಗೆ ಬರುವ ಸಾಧ್ಯತೆ: " + value)
        else:
            output.append(line)

    # Add the MongoDB OTT date at the end, without replacing provider platform data.
    if fallback_date:
        output.append("India OTT Release Date: " + fallback_date)
    elif not date_line_added:
        output.append("India OTT Release Date: " + (current_date or "Not Announced Yet"))

    if not any(line.startswith("📌 ಟೆಲಿಗ್ರಾಂಗೆ ಬರುವ ಸಾಧ್ಯತೆ:") for line in output):
        value = fallback_date or (current_date if not is_missing(current_date) else "ಬಂದಿಲ್ಲ")
        output.append("📌 ಟೆಲಿಗ್ರಾಂಗೆ ಬರುವ ಸಾಧ್ಯತೆ: " + value)

    return output

def apply_saved_theatrical_date(release_lines, saved):
    """Use the manually saved RDATE for a missing theatrical date or series first-air date."""
    lines = list(release_lines or [])
    saved_date = (saved or {}).get("theatrical_release_date")
    if not saved_date:
        return lines

    missing_values = {"", "not announced yet", "not available", "n/a", "not announced", "ಬಂದಿಲ್ಲ"}
    date_labels = (
        "India Theatrical Release Date:",
        "🇮🇳 India Theatrical Release Date:",
        "India First Air Date:",
        "🇮🇳 India First Air Date:",
        "Release Date:",
    )
    for index, line in enumerate(lines):
        if line.startswith(date_labels):
            current = line.split(":", 1)[1].strip() if ":" in line else ""
            if current.casefold() in missing_values:
                label = line.split(":", 1)[0]
                lines[index] = label + ": " + saved_date
            return lines

    lines.append("Theatrical Release Date: " + saved_date)
    return lines


def save_movie_post(message):
    caption = (message.caption or message.text or "").strip()
    if not caption:
        return False
    first_line = next((line.strip() for line in caption.splitlines() if line.strip()), "")
    first_line = re.sub(r"<[^>]+>", "", first_line)
    first_line = re.sub(r"^[^\w(]+", "", first_line).strip()
    # Keep the movie title clean when OTT date is appended to the same line.
    first_line = re.sub(
        r"\s*[–—-]\s*India\s+OTT\s+(?:Release\s+)?Date\s*:\s*.+$",
        "",
        first_line,
        flags=re.IGNORECASE,
    ).strip()
    title = first_line
    title_base = normalize_movie_key(clean_search_title(title))
    if not title_base:
        return False
    year_match = re.search(r"\b((?:19|20)\d{2})\b", title)
    year = year_match.group(1) if year_match else ""
    photo_file_id = message.photo[-1].file_id if message.photo else None
    ott_platform, ott_release_date = extract_ott_info(caption)
    record = {
        "title": title,
        "title_base": title_base,
        "year": year,
        "caption": caption,
        "ott_platform": ott_platform,
        "ott_release_date": ott_release_date,
        "photo_file_id": photo_file_id,
        "channel_id": int(message.chat_id),
        "message_id": int(message.message_id),
        "updated_at": datetime.utcnow(),
    }
    with db_connect() as conn:
        if not hasattr(conn, "movies"):
            raise RuntimeError("Movie Channel storage requires the configured MongoDB connection.")
        conn.movies.update_one(
            {"title_base": title_base, "year": year},
            {"$set": record},
            upsert=True,
        )
    return True


def update_saved_ott_date(message):
    """Update OTT Date or theatrical RDATE from a Delete Channel message."""
    caption = (message.caption or message.text or "").strip()
    if not caption:
        return False

    first_line = next((line.strip() for line in caption.splitlines() if line.strip()), "")
    first_line = re.sub(r"<[^>]+>", "", first_line)
    first_line = re.sub(r"^[^\w(]+", "", first_line).strip()

    rdate_match = re.search(r"\bRDATE\s*:\s*(.+)$", first_line, re.IGNORECASE)
    ott_date_match = re.search(r"\bDate\s*:\s*(.+)$", first_line, re.IGNORECASE) if not rdate_match else None
    if rdate_match:
        field_name = "theatrical_release_date"
        release_date = rdate_match.group(1).strip()
    elif ott_date_match:
        field_name = "ott_release_date"
        release_date = ott_date_match.group(1).strip()
    else:
        return False

    title = first_line[:rdate_match.start() if rdate_match else ott_date_match.start()].strip()
    title = re.sub(r"[–—-]\s*$", "", title).strip()
    year_match = re.search(r"\b((?:19|20)\d{2})\b", title)
    if not year_match:
        return False

    title_base = normalize_movie_key(clean_search_title(title))
    if not title_base or not release_date:
        return False

    year = year_match.group(1)
    with db_connect() as conn:
        if not hasattr(conn, "movies"):
            raise RuntimeError("Date updates require the configured MongoDB connection.")
        result = conn.movies.update_one(
            {"title_base": title_base, "year": year},
            {"$set": {field_name: release_date, "updated_at": datetime.utcnow()}},
        )
    return result.matched_count > 0



def delete_saved_movie_record(message):
    """Delete one movie's saved MongoDB record from an explicit Delete Channel command."""
    caption = (message.caption or message.text or "").strip()
    if not caption:
        return False

    first_line = next((line.strip() for line in caption.splitlines() if line.strip()), "")
    first_line = re.sub(r"<[^>]+>", "", first_line).strip()
    match = re.match(r"^DELETE\s*:\s*(.+)$", first_line, re.IGNORECASE)
    if not match:
        return False

    title = match.group(1).strip()
    year_match = re.search(r"\b((?:19|20)\d{2})\b", title)
    if not year_match:
        return False

    title_base = normalize_movie_key(clean_search_title(title))
    year = year_match.group(1)
    if not title_base:
        return False

    with db_connect() as conn:
        if not hasattr(conn, "movies"):
            raise RuntimeError("Movie deletion requires the configured MongoDB connection.")
        result = conn.movies.delete_one({"title_base": title_base, "year": year})
    return result.deleted_count > 0


def find_saved_movie(query):
    query_title = clean_search_title(query)
    title_base = normalize_movie_key(query_title)
    if not title_base:
        return None
    year_match = re.search(r"\b((?:19|20)\d{2})\b", query)
    year = year_match.group(1) if year_match else ""
    with db_connect() as conn:
        if not hasattr(conn, "movies"):
            return None
        if year:
            found = conn.movies.find_one({"title_base": title_base, "year": year})
            if found:
                return found
        return conn.movies.find_one(
            {"title_base": title_base},
            sort=[("updated_at", -1)],
        )


def is_ai_enabled():
    """AI switch is stored in the existing persistent settings database."""
    with db_connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            ("ai_enabled",),
        ).fetchone()
    return bool(row and str(row[0]).strip().lower() in {"1", "true", "yes", "on"})


def set_ai_enabled(enabled):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("ai_enabled", "true" if enabled else "false"),
        )


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


async def omdb_get(params):
    if not OMDB_API_KEY:
        return None
    query = {"apikey": OMDB_API_KEY, **params}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(OMDB_BASE, params=query)
        response.raise_for_status()
        data = response.json()
    if data.get("Response") != "True":
        return None
    return data


async def find_imdb_details(query):
    """Use OMDb's IMDb-linked catalogue only when TMDB has no exact title match."""
    search_title = clean_search_title(query)
    requested_year = re.search(r"\b((?:19|20)\d{2})\b", query)
    params = {"s": search_title, "page": 1}
    results = await omdb_get(params)
    if not results:
        return None
    normalized_query = re.sub(r"[^a-z0-9]+", "", search_title.casefold())
    candidates = []
    for item in results.get("Search", []):
        title = item.get("Title") or ""
        normalized_title = re.sub(r"[^a-z0-9]+", "", title.casefold())
        if normalized_title == normalized_query and item.get("imdbID"):
            year = (item.get("Year") or "")[:4]
            year_score = 1 if requested_year and year == requested_year.group(1) else 0
            candidates.append((year_score, item))
    if not candidates:
        return None
    candidates.sort(key=lambda row: row[0], reverse=True)
    full = await omdb_get({"i": candidates[0][1]["imdbID"], "plot": "full"})
    if not full:
        return None
    poster = full.get("Poster")
    if poster == "N/A":
        poster = None
    return {
        "title": full.get("Title") or candidates[0][1].get("Title") or search_title,
        "year": (full.get("Year") or "N/A")[:4],
        "date": full.get("Released") or "Not announced",
        "poster": poster,
        "poster_url": poster,
        "overview": full.get("Plot") if full.get("Plot") != "N/A" else "Synopsis not available.",
        "rating": (full.get("imdbRating") + "/10") if full.get("imdbRating") not in (None, "N/A") else "N/A",
        "rating_label": "IMDb Rating",
        "genres": full.get("Genre") if full.get("Genre") != "N/A" else "N/A",
        "cast": full.get("Actors") if full.get("Actors") != "N/A" else "N/A",
        "languages": full.get("Language") if full.get("Language") != "N/A" else "Not Available",
        "imdb_id": full.get("imdbID"),
        "kind": "Web Series" if full.get("Type") == "series" else "Movie",
    }


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


def clean_search_title(query):
    """Remove a trailing release year such as (2026) before title matching."""
    return re.sub(r"\s*\(?\b(?:19|20)\d{2}\)?\s*$", "", query).strip()


async def find_title(query):
    """Search movies and TV shows; treat a year in brackets as a hint, not part of the title."""
    search_title = clean_search_title(query)
    year_match = re.search(r"\b((?:19|20)\d{2})\b", query)
    requested_year = year_match.group(1) if year_match else None
    found = []
    for kind, endpoint in (("movie", "/search/movie"), ("tv", "/search/tv")):
        data = await tmdb_get(endpoint, {"query": search_title, "include_adult": "false", "page": 1})
        for item in data.get("results", [])[:20]:
            name = item.get("title") if kind == "movie" else item.get("name")
            if not name:
                continue
            ratio = title_similarity(search_title, name)
            date = item.get("release_date") if kind == "movie" else item.get("first_air_date")
            year_match_score = 1 if requested_year and date and date[:4] == requested_year else 0
            found.append((ratio, year_match_score, kind, item))
    if not found:
        return None
    found.sort(key=lambda row: (row[0], row[1], float(row[3].get("popularity") or 0)), reverse=True)
    return found[0][2], found[0][3], found[0][0]


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
        "overview": overview, "rating": f"{float(rating):.1f}/10" if rating is not None else "N/A",
        "genres": genres, "cast": cast,
        "languages": ", ".join(lang.get("english_name") or lang.get("name", "") for lang in details.get("spoken_languages", []) if lang.get("english_name") or lang.get("name")) or "Not Available",
        "imdb_id": imdb_id,
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
            release_lines.append("India Theatrical Release Date: " + (format_tmdb_date(theatrical[0]) if theatrical else "Not Announced Yet"))
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
        provider_lines.append("India OTT Platform: " + (", ".join(names) if names else "Not Announced Yet"))
        display_ott_date = format_tmdb_date(india_ott_date) if india_ott_date != "Not Announced Yet" else "ಬಂದಿಲ್ಲ"
        provider_lines.append("📌 ಟೆಲಿಗ್ರಾಂಗೆ ಬರುವ ಸಾಧ್ಯತೆ: " + display_ott_date)
        provider_lines.append("India OTT Release Date: " + (format_tmdb_date(india_ott_date) if india_ott_date != "Not Announced Yet" else "Not Announced Yet"))
    except Exception as exc:
        log.warning("India OTT lookup unavailable (%s)", type(exc).__name__)
        provider_lines.append("India OTT Platform: Not Announced Yet")
        display_ott_date = format_tmdb_date(india_ott_date) if india_ott_date != "Not Announced Yet" else "ಬಂದಿಲ್ಲ"
        provider_lines.append("📌 ಟೆಲಿಗ್ರಾಂಗೆ ಬರುವ ಸಾಧ್ಯತೆ: " + display_ott_date)
        provider_lines.append("India OTT Release Date: " + (format_tmdb_date(india_ott_date) if india_ott_date != "Not Announced Yet" else "Not Announced Yet"))
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

    # User-managed Telegram Movie Database is checked before online APIs.
    try:
        saved = find_saved_movie(query)
        if saved and not (saved.get("ott_platform") or saved.get("ott_release_date") or saved.get("theatrical_release_date")):
            bot = message.get_bot()
            # Re-send the saved poster and make every user-provided caption line bold.
            bold_caption = "<b>" + html.escape(saved.get("caption") or saved.get("title") or "") + "</b>"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬇️ Download", url=movie_link(clean_search_title(saved.get("title") or query)))
            ]])
            photo_id = saved.get("photo_file_id")
            if photo_id:
                if len(bold_caption) <= 1024:
                    sent = await bot.send_photo(
                        chat_id=message.chat_id,
                        photo=photo_id,
                        caption=bold_caption,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                    schedule_auto_delete(sent, bot)
                else:
                    sent = await bot.send_photo(
                        chat_id=message.chat_id,
                        photo=photo_id,
                        reply_markup=keyboard,
                    )
                    schedule_auto_delete(sent, bot)
                    sent_text = await bot.send_message(
                        chat_id=message.chat_id,
                        text=bold_caption[:4000],
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    schedule_auto_delete(sent_text, bot)
            else:
                sent = await bot.send_message(
                    chat_id=message.chat_id,
                    text=bold_caption[:4000],
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                schedule_auto_delete(sent, bot)
            return True
    except Exception as exc:
        log.warning("Movie database lookup/copy failed; falling back to APIs (%s)", type(exc).__name__)

    result = None
    try:
        result = await find_title(query)
    except Exception as exc:
        log.warning("TMDB search unavailable; trying IMDb fallback (%s)", type(exc).__name__)

    details = None
    release_lines, provider_lines = [], []
    if result:
        # Accept TMDB only when the title matches exactly (ignoring punctuation/year).
        kind, item, similarity = result
        matched_title = item.get("title") if kind == "movie" else item.get("name")
        clean_query = re.sub(r"[^a-z0-9]+", "", clean_search_title(query).casefold())
        clean_title = re.sub(r"[^a-z0-9]+", "", (matched_title or "").casefold())
        if clean_query and clean_query == clean_title:
            try:
                details = await title_details(kind, item)
                release_lines, provider_lines = await release_and_ott(kind, item["id"])
                release_lines = apply_saved_theatrical_date(release_lines, saved)
                provider_lines = apply_saved_ott_info(provider_lines, saved)
            except Exception as exc:
                log.warning("TMDB details unavailable; trying IMDb fallback (%s)", type(exc).__name__)
                details = None

    # If TMDB has no exact match (or is unavailable), search IMDb via OMDb.
    if details is None:
        try:
            details = await find_imdb_details(query)
        except Exception as exc:
            log.warning("IMDb fallback unavailable (%s)", type(exc).__name__)
        if details is None:
            return await show_title_suggestions(message, query)
        release_lines = apply_saved_theatrical_date([f"Release Date: {details['date']}"], saved)
        provider_lines = apply_saved_ott_info(
            ["India OTT Platform: Not Available from IMDb"], saved
        )
    lines = [
        f"🎬 <b>{html.escape(details['title'])} ({html.escape(details['year'])})</b>",
        f"📌 <b>Type:</b> {details['kind']}",
        f"⭐ <b>{html.escape(details.get('rating_label', 'TMDb Rating'))}:</b> {html.escape(details['rating'])}",
        f"🎭 <b>Genre:</b> {html.escape(details['genres'])}",
        f"🌐 Available Languages: {html.escape(details['languages'])}",
        f"👥 <b>Cast:</b> {html.escape(details['cast'])}",
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
    poster_url = details.get("poster_url") or (f"https://image.tmdb.org/t/p/w500{poster}" if poster else None)
    if poster_url and len(caption) <= 1024:
        await reply_photo_auto(message, photo=poster_url, caption=caption, parse_mode="HTML", reply_markup=keyboard)
    elif poster_url:
        await reply_photo_auto(message, photo=poster_url, reply_markup=keyboard)
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
    origin = message.forward_origin
    source_chat = getattr(origin, "chat", None) if origin else None
    if context.user_data.get("waiting_for_database_channel"):
        if not source_chat or source_chat.type != "channel":
            await reply_text_auto(message, "ದಯವಿಟ್ಟು Movie Database Channel‌ನ original post ಅನ್ನು Forward ಮಾಡಿ.")
            return
        set_database_channel(source_chat.id)
        context.user_data.pop("waiting_for_database_channel", None)
        await reply_text_auto(
            message,
            f"✅ Movie Database Channel set ಆಯಿತು.\\nChannel ID: <code>{source_chat.id}</code>\\nBotಗೆ ಆ Channelನಲ್ಲಿ Admin permission ಕೊಡು.",
            parse_mode="HTML",
        )
        return
    if not context.user_data.get("waiting_for_source_channel"):
        return
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


async def movie_database_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message or message.chat_id != get_database_channel():
        return
    try:
        saved = save_movie_post(message)
        if saved:
            log.info("Movie database entry saved/updated: channel=%s message=%s", message.chat_id, message.message_id)
    except Exception as exc:
        log.exception("Could not save movie database post (%s)", type(exc).__name__)


async def movie_ott_update_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message or message.chat_id != DELETE_CHANNEL_ID:
        return
    try:
        caption = (message.caption or message.text or "").strip()
        if re.match(r"^\s*DELETE\s*:", caption, re.IGNORECASE):
            deleted = delete_saved_movie_record(message)
            if deleted:
                log.info("Movie database record deleted from Delete Channel: message=%s", message.message_id)
            else:
                log.warning("Movie delete command ignored: movie/year not found (message=%s)", message.message_id)
            return

        updated = update_saved_ott_date(message)
        if updated:
            log.info("Movie OTT date updated from Delete Channel: message=%s", message.message_id)
        else:
            log.warning("OTT date update ignored: movie/year not found or date missing (message=%s)", message.message_id)
    except Exception as exc:
        log.exception("Could not process Delete Channel command (%s)", type(exc).__name__)


async def mdb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or update.effective_user.id != OWNER_ID:
        return
    current = get_database_channel()
    await reply_text_auto(
        message,
        f"<b>Movie Database Channel</b>\\nChannel ID: <code>{current}</code>\\n\\nಈ Channel ID ಅನ್ನು bot codeನಲ್ಲಿ fixed ಆಗಿ ಇಟ್ಟಿದ್ದೇವೆ. ಮತ್ತೆ /mdb ಮೂಲಕ set ಮಾಡುವ ಅಗತ್ಯವಿಲ್ಲ. Botಗೆ ಆ Channelನಲ್ಲಿ Admin permission ಇರಬೇಕು.",
        parse_mode="HTML",
    )


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


async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Private-message OpenRouter AI command; enable state persists in MongoDB settings."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user or message.chat.type != "private":
        return

    args = context.args or []
    action = args[0].lower() if args else ""

    if action in {"on", "off"}:
        if user.id != OWNER_ID:
            await message.reply_text("ಈ setting Owner ಮಾತ್ರ ಬದಲಾಯಿಸಬಹುದು.")
            return
        if action == "on":
            if not os.getenv("OPENROUTER_API_KEY", "").strip():
                await message.reply_text(
                    "OpenRouter API key configure ಆಗಿಲ್ಲ. Render-ನಲ್ಲಿ OPENROUTER_API_KEY secret ಸೇರಿಸಿ."
                )
                return
            set_ai_enabled(True)
            await message.reply_text("✅ OpenRouter AI ON ಆಯಿತು. ಇದು restart ಆದರೂ ON ಆಗಿಯೇ ಇರುತ್ತದೆ.")
        else:
            set_ai_enabled(False)
            await message.reply_text("⛔ OpenRouter AI OFF ಆಯಿತು.")
        return

    if action == "status":
        state = "ON" if is_ai_enabled() else "OFF"
        await message.reply_text(f"OpenRouter AI Status: {state}")
        return

    if not args:
        await message.reply_text(
            "ಬಳಕೆ:\n/ai on - AI ON (Owner ಮಾತ್ರ)\n/ai off - AI OFF (Owner ಮಾತ್ರ)\n"
            "/ai status - Status\n/ai ನಿಮ್ಮ ಪ್ರಶ್ನೆ - AI ಜೊತೆ chat"
        )
        return

    if not is_ai_enabled():
        await message.reply_text("OpenRouter AI ಈಗ OFF ಇದೆ.")
        return

    prompt = " ".join(args).strip()
    if len(prompt) > 3500:
        await message.reply_text("ಪ್ರಶ್ನೆ ತುಂಬಾ ಉದ್ದವಾಗಿದೆ. ದಯವಿಟ್ಟು ಚಿಕ್ಕದಾಗಿ ಕಳುಹಿಸಿ.")
        return

    status = await message.reply_text("🤖 ಉತ್ತರ ಸಿದ್ಧಪಡಿಸಲಾಗುತ್ತಿದೆ...")
    try:
        from openrouter_ai import generate_answer
        answer = await generate_answer(prompt)
        await status.edit_text((answer or "ಕ್ಷಮಿಸಿ, ಉತ್ತರ ಸಿಗಲಿಲ್ಲ.")[:3900])
    except Exception as exc:
        error_code = getattr(exc, "code", None) or getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        error_text = str(exc).lower()
        log.exception("OpenRouter request failed (code=%s)", error_code)

        if error_code == 429 or "quota" in error_text or "resource_exhausted" in error_text:
            user_error = "OpenRouter Free request limit ಅಥವಾ model quota ತಲುಪಿದೆ. ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ."
        elif error_code in (401, 403) or "api key" in error_text or "permission_denied" in error_text:
            user_error = "OpenRouter API key ತಪ್ಪಾಗಿದೆ ಅಥವಾ API access ಇಲ್ಲ. Render‌ನ OPENROUTER_API_KEY ಪರಿಶೀಲಿಸಿ."
        elif error_code == 404 or "not_found" in error_text:
            user_error = "OpenRouter Free model ಲಭ್ಯವಿಲ್ಲ. ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ."
        elif error_code in (408, 500, 502, 503, 504) or any(
            term in error_text for term in ("unavailable", "overloaded", "timeout")
        ):
            user_error = "OpenRouter Free AI model ಈಗ busy ಇದೆ. ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ."
        elif error_code == 402:
            user_error = "OpenRouter account balance ಸಮಸ್ಯೆಯಿಂದ Free model ಬಳಸಲು ಆಗುತ್ತಿಲ್ಲ. OpenRouter account ಪರಿಶೀಲಿಸಿ."
        else:
            user_error = "OpenRouter API ಸಂಪರ್ಕದಲ್ಲಿ ಸಮಸ್ಯೆಯಾಗಿದೆ. Render Logs ನೋಡಿ."
        await status.edit_text(user_error)


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
    if not MONGODB_URI:
        missing.append("MONGODB_URI")
    if missing:
        raise SystemExit("Missing configuration: " + ", ".join(missing))
    app = ApplicationBuilder().token(TOKEN).post_init(startup_notify).build()
    # React to incoming PM and group messages without interrupting existing handlers.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, react_to_message), group=-2)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, react_to_message), group=-2)
    app.add_handler(CommandHandler("ai", ai_command))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fs", fs_command))
    app.add_handler(CommandHandler("mdb", mdb_command))
    app.add_handler(CallbackQueryHandler(fs_callback, pattern=r"^fs:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.FORWARDED, accept_source_forward), group=-1)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_reply))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.TEXT & ~filters.COMMAND, private_non_text))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.REPLY, source_discussion_reply), group=-1)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_check))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, source_channel_reply))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, movie_database_channel_post), group=1)
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, movie_ott_update_channel_post), group=2)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
