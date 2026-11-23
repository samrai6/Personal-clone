import logging
import os

from google import genai

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


async def generate_answer(prompt: str) -> str:
    """Generate one answer; command permissions and ON/OFF state live in bot.py."""
    response = await get_client().aio.models.generate_content(
        model=MODEL,
        contents=prompt,
        config={"max_output_tokens": 700},
    )
    return (response.text or "").strip()
