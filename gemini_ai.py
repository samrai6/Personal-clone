import logging
import os

from google import genai
from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
_client = None


def get_client():
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        _client = genai.Client(api_key=api_key)
    return _client


async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Optional one-shot Gemini command. Registered only when explicitly enabled."""
    message = update.effective_message
    if not message:
        return

    prompt = " ".join(context.args).strip()
    if not prompt:
        await message.reply_text("ಬಳಕೆ: /ai ನಿಮ್ಮ ಪ್ರಶ್ನೆ")
        return

    status = await message.reply_text("🤖 ಉತ್ತರ ಸಿದ್ಧಪಡಿಸಲಾಗುತ್ತಿದೆ...")
    try:
        response = await get_client().aio.models.generate_content(
            model=MODEL,
            contents=prompt,
            config={"max_output_tokens": 700, "temperature": 0.7},
        )
        answer = (response.text or "").strip()
        if not answer:
            answer = "ಕ್ಷಮಿಸಿ, ಉತ್ತರ ಸಿಗಲಿಲ್ಲ. ಮತ್ತೊಮ್ಮೆ ಪ್ರಯತ್ನಿಸಿ."
        # Telegram text messages have a 4096-character limit.
        await status.edit_text(answer[:3900])
    except Exception as exc:
        log.warning("Gemini request failed (%s)", type(exc).__name__)
        await status.edit_text(
            "Gemini AI ಈಗ ಲಭ್ಯವಿಲ್ಲ ಅಥವಾ free quota ಮುಗಿದಿದೆ. ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಪ್ರಯತ್ನಿಸಿ."
        )
