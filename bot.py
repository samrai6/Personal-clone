import html, json, logging, os, re
from google import genai
from google.genai import types
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
from quota import DailyQuota

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGODB_URI", "")
DB_NAME = os.getenv("MONGODB_NAME", "personal_clone")
API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MOVIE_BOT = os.getenv("MOVIE_BOT_USERNAME", "Samp_yu5bot").lstrip("@")
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
mongo = None
quota = None
ai = None


def parse_json(text):
    text = re.sub(r"^\s*```(?:json)?|\```\s*$", "", (text or "").strip(), flags=re.I)
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
    return None


async def gemini(prompt):
    result = await ai.aio.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        ),
    )
    return result.text or ""


def movie_link(title):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
    payload = ("getfile-" + slug)[:64].strip("-")
    return "https://t.me/" + MOVIE_BOT + "?start=" + payload


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ನಮಸ್ಕಾರ! ಕನ್ನಡ ಅಥವಾ Englishನಲ್ಲಿ message ಮಾಡಿ. Movies, Web Series ಮತ್ತು ಸಾಮಾನ್ಯ ಪ್ರಶ್ನೆಗಳಿಗೆ ಸಹಾಯ ಮಾಡುತ್ತೇನೆ."
    )


async def private_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quota
    msg, user = update.effective_message, update.effective_user
    if not msg or not msg.text or not user:
        return
    if user.id not in ADMINS:
        allowed, used = await quota.consume(user.id)
        if not allowed:
            await msg.reply_text("ನಿಮ್ಮ ಇಂದಿನ 50 AI messages ಮುಗಿದಿವೆ. ನಾಳೆ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.")
            return
    status = await msg.reply_text("⏳ ಉತ್ತರ ಹುಡುಕುತ್ತಿದ್ದೇನೆ...")
    prompt = f"""
You are a helpful bilingual Kannada/English Telegram assistant.
User message: {msg.text!r}
Use Google Search grounding for current movie and OTT facts. Do not invent cast, ratings,
OTT platforms, or dates. If an OTT date is not officially announced, say Not announced.
If reliable sources report an estimated OTT date, label it Unconfirmed estimate.
If the message is only a movie/web-series title, return JSON:
{{"kind":"movie","title":"official title","language":"...","release":"theatre date or Unknown","cast":"...","plot":"one-line spoiler-free plot","rating":"rating and source or Unknown","ott_status":"...","ott_platform":"...","ott_date":"official date or Not announced","expected_ott_date":"estimate or Not announced","reply":"short response"}}
For any other message return JSON: {{"kind":"chat","reply":"helpful answer in user's language"}}.
Return JSON only.
"""
    try:
        data = parse_json(await gemini(prompt))
        if not isinstance(data, dict):
            answer, title = "ಕ್ಷಮಿಸಿ, ಉತ್ತರ ಸಿದ್ಧಪಡಿಸಲು ಆಗಲಿಲ್ಲ. ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.", None
        elif data.get("kind") == "movie" and data.get("title"):
            title = str(data["title"]).strip()
            fields = [
                ("Language", "language"), ("Theatre Release", "release"),
                ("Cast", "cast"), ("Story", "plot"), ("Rating", "rating"),
                ("OTT Status", "ott_status"), ("OTT Platform", "ott_platform"),
                ("OTT Release Date", "ott_date"), ("Expected OTT Date", "expected_ott_date")
            ]
            lines = ["🎬 <b>" + html.escape(title) + "</b>"]
            for label, key in fields:
                value = str(data.get(key) or "").strip()
                if value and value.lower() not in ("unknown", "not announced", "n/a"):
                    lines.append("<b>" + label + ":</b> " + html.escape(value))
                elif key in ("ott_date", "expected_ott_date"):
                    lines.append("<b>" + label + ":</b> Not announced")
            if data.get("reply"):
                lines.extend(["", html.escape(str(data["reply"]))])
            answer = "\n".join(lines)
        else:
            title = None
            answer = str(data.get("reply") or "ಕ್ಷಮಿಸಿ, ಉತ್ತರ ಸಿದ್ಧಪಡಿಸಲು ಆಗಲಿಲ್ಲ.")
        keyboard = None
        if title:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Search in @" + MOVIE_BOT, url=movie_link(title))
            ]])
        await status.edit_text(answer, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
    except Exception:
        log.exception("PM handler failed")
        await status.edit_text("ಕ್ಷಮಿಸಿ, ಈಗ ತಾಂತ್ರಿಕ ಸಮಸ್ಯೆ ಇದೆ. ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಪ್ರಯತ್ನಿಸಿ.")


async def group_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if len(text) < 2 or len(text) > 90 or len(text.split()) > 8 or text.startswith("/"):
        return
    prompt = f"""
Is this text a movie or web-series title, perhaps misspelled? {text!r}
Use Google Search grounding to verify the official spelling. Be conservative.
Return JSON only: {{"is_title":true/false,"correct_title":"verified title","confidence":0.0}}.
Only set confidence >= 0.90 if verified. Never guess.
"""
    try:
        data = parse_json(await gemini(prompt))
        if not isinstance(data, dict) or not data.get("is_title"):
            return
        correct = str(data.get("correct_title") or "").strip()
        confidence = float(data.get("confidence") or 0)
        if not correct or confidence < 0.90 or correct.casefold() == text.casefold():
            return
        await msg.reply_text(
            "ನಿಮ್ಮ ಮೂವೀ ಸ್ಪೆಲ್ಲಿಂಗ್ ತಪ್ಪಾಗಿದೆ\nYour movie spelling is wrong\n\nCorrect spelling: " + correct,
            reply_to_message_id=msg.message_id,
        )
    except Exception:
        log.exception("Group spelling check failed")


async def post_init(app):
    global mongo, quota, ai
    mongo = AsyncIOMotorClient(MONGO_URI)
    db = mongo[DB_NAME]
    await db.command("ping")
    quota = DailyQuota(db)
    await quota.initialize()
    ai = genai.Client(api_key=API_KEY)
    log.info("MongoDB and Gemini initialized")


async def post_shutdown(app):
    if mongo:
        mongo.close()


def main():
    missing = [k for k, v in {"BOT_TOKEN": TOKEN, "MONGODB_URI": MONGO_URI, "GEMINI_API_KEY": API_KEY}.items() if not v]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_reply))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_check))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
