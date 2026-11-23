import html
import logging
import os
import re
from difflib import SequenceMatcher

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN", "")
# Set TMDB_API_KEY in Render. Do not commit a real key to this public repository.
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "PASTE_TMDB_API_KEY_HERE")
MOVIE_BOT = "Samp_yu5bot"
TMDB_BASE = "https://api.themoviedb.org/3"
LANGUAGE = "en-US"
COUNTRY = "IN"


async def tmdb_get(path, params=None):
    query = dict(params or {})
    query["api_key"] = TMDB_API_KEY
    query.setdefault("language", LANGUAGE)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(TMDB_BASE + path, params=query)
        response.raise_for_status()
        return response.json()


def movie_link(title):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title.strip()).strip("-") or "movie"
    return f"https://t.me/{MOVIE_BOT}?start=" + ("getfile-" + slug)[:64].strip("-")


async def find_title(query):
    """Search both movies and TV shows; prefer the strongest title match."""
    found = []
    for kind, endpoint in (("movie", "/search/movie"), ("tv", "/search/tv")):
        data = await tmdb_get(endpoint, {"query": query, "include_adult": "false", "page": 1})
        for item in data.get("results", [])[:5]:
            name = item.get("title") if kind == "movie" else item.get("name")
            if not name:
                continue
            ratio = SequenceMatcher(None, query.casefold(), name.casefold()).ratio()
            found.append((ratio, kind, item))
    if not found:
        return None
    found.sort(key=lambda row: (row[0], float(row[2].get("popularity") or 0)), reverse=True)
    return found[0][1], found[0][2], found[0][0]


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


async def show_movie(message, query, group_check=False):
    result = await find_title(query)
    if not result:
        if not group_check:
            await message.reply_text("TMDBನಲ್ಲಿ ಈ Movie / Web Series ಸಿಗಲಿಲ್ಲ. Title spelling ಪರಿಶೀಲಿಸಿ.")
        return

    kind, item, similarity = result
    canonical = item.get("title") if kind == "movie" else item.get("name")
    if not canonical:
        return

    # In groups, only reply when TMDB suggests a likely spelling correction.
    if group_check:
        if canonical.casefold() == query.casefold() or similarity < 0.72:
            return
        await message.reply_text(
            "ನಿಮ್ಮ ಮೂವೀ ಸ್ಪೆಲ್ಲಿಂಗ್ ತಪ್ಪಾಗಿದೆ\nYour movie spelling is wrong\n\nCorrect spelling: " + canonical,
            reply_to_message_id=message.message_id,
        )
        return

    details = await title_details(kind, item)
    lines = [
        f"🎬 <b>{html.escape(details['title'])} ({html.escape(details['year'])})</b>",
        f"⭐ <b>Rating:</b> {html.escape(details['rating'])}",
        f"📌 <b>Type:</b> {details['kind']}",
        f"📅 <b>Release:</b> {html.escape(details['date'])}",
        f"🎭 <b>Genre:</b> {html.escape(details['genres'])}",
        f"👥 <b>Cast:</b> {html.escape(details['cast'])}",
        "",
        html.escape(details["overview"]),
    ]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 Search in @Samp_yu5bot", url=movie_link(details["title"]))
    ]])
    caption = "\\n".join(lines)
    poster = details.get("poster")
    if poster:
        await message.reply_photo(
            photo=f"https://image.tmdb.org/t/p/w500{poster}",
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        await message.reply_text(
            caption,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "ನಮಸ್ಕಾರ! Movie ಅಥವಾ Web Series ಹೆಸರು ಕಳುಹಿಸಿ. TMDB details ಮತ್ತು IMDb Search link ಕೊಡುತ್ತೇನೆ."
    )


async def private_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return
    query = message.text.strip()
    if len(query) < 2 or len(query) > 100:
        return
    try:
        await show_movie(message, query)
    except httpx.HTTPStatusError as exc:
        log.warning("TMDB request failed with HTTP %s", exc.response.status_code)
        await message.reply_text("TMDB search ಈಗ ಲಭ್ಯವಿಲ್ಲ. ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಪ್ರಯತ್ನಿಸಿ.")
    except Exception as exc:
        log.error("Movie lookup failed (%s)", type(exc).__name__)
        await message.reply_text("ಕ್ಷಮಿಸಿ, Movie details ಹುಡುಕಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ.")


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
        log.warning("Group TMDB title check failed (%s)", type(exc).__name__)


def main():
    missing = []
    if not TOKEN:
        missing.append("BOT_TOKEN")
    if not TMDB_API_KEY or TMDB_API_KEY == "PASTE_TMDB_API_KEY_HERE":
        missing.append("TMDB_API_KEY")
    if missing:
        raise SystemExit("Missing configuration: " + ", ".join(missing))
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_reply))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_check))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
