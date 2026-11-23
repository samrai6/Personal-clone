import asyncio
import logging
import os
import random

from google import genai

log = logging.getLogger(__name__)

# Current stable Flash model. Can be overridden without changing the source.
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


def _error_code(exc):
    return (
        getattr(exc, "code", None)
        or getattr(exc, "status_code", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
    )


async def generate_answer(prompt: str) -> str:
    """Generate an answer, retrying temporary Google API failures."""
    client = get_client()
    for attempt in range(3):
        try:
            response = await client.aio.models.generate_content(
                model=MODEL,
                contents=prompt,
                config={"max_output_tokens": 700},
            )
            answer = (response.text or "").strip()
            if not answer:
                raise RuntimeError("Gemini returned an empty response")
            return answer
        except Exception as exc:
            code = _error_code(exc)
            message = str(exc).lower()
            retryable = (
                code in (408, 429, 500, 502, 503, 504)
                or any(term in message for term in (
                    "unavailable", "overloaded", "temporarily", "timeout",
                    "resource_exhausted", "rate limit",
                ))
            )
            if not retryable or attempt == 2:
                log.exception(
                    "Gemini request failed after %s attempt(s); code=%s model=%s",
                    attempt + 1, code, MODEL,
                )
                raise
            delay = min(2 ** (attempt + 1), 8) + random.uniform(0, 0.5)
            log.warning(
                "Temporary Gemini API error (code=%s); retry %s/2 in %.1fs",
                code, attempt + 1, delay,
            )
            await asyncio.sleep(delay)
