import asyncio
import logging
import os
import random

from google import genai

log = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash")
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


def _is_temporary_server_error(exc):
    code = _error_code(exc)
    message = str(exc).lower()
    return code in (500, 502, 503, 504) or any(
        term in message for term in ("unavailable", "overloaded", "temporarily")
    )


async def _generate_with_retries(client, model, prompt):
    for attempt in range(3):
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config={"max_output_tokens": 700},
            )
            answer = (response.text or "").strip()
            if not answer:
                raise RuntimeError("Gemini returned an empty response")
            return answer
        except Exception as exc:
            code = _error_code(exc)
            if not _is_temporary_server_error(exc) or attempt == 2:
                log.exception(
                    "Gemini request failed; model=%s attempt=%s code=%s",
                    model, attempt + 1, code,
                )
                raise
            delay = min(2 ** (attempt + 1), 8) + random.uniform(0, 0.5)
            log.warning(
                "Gemini server busy; model=%s retry=%s/2 delay=%.1fs",
                model, attempt + 1, delay,
            )
            await asyncio.sleep(delay)


async def generate_answer(prompt: str) -> str:
    """Try the primary model, then a free-tier Flash fallback on server overload."""
    client = get_client()
    models = list(dict.fromkeys([MODEL, FALLBACK_MODEL]))
    for index, model in enumerate(models):
        try:
            return await _generate_with_retries(client, model, prompt)
        except Exception as exc:
            # Do not switch models for bad keys, missing models, or quota errors.
            # A fallback is useful specifically when Google's serving capacity is busy.
            if index == len(models) - 1 or not _is_temporary_server_error(exc):
                raise
            log.warning(
                "Primary Gemini model %s is unavailable; trying fallback model %s",
                model, models[index + 1],
            )
    raise RuntimeError("All configured Gemini models are unavailable")
