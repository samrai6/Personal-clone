import asyncio
import logging
import os
import random

import httpx

log = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
_client = None


class OpenRouterAPIError(RuntimeError):
    def __init__(self, status_code, message):
        super().__init__(f"OpenRouter API error {status_code}: {message}")
        self.code = status_code


def get_api_key():
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")
    return key


async def generate_answer(prompt: str) -> str:
    """Generate text using OpenRouter's automatically selected free model."""
    global _client
    api_key = get_api_key()
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/samrai6/Personal-clone",
        "X-Title": "Personal Clone Telegram Bot",
    }
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 700,
    }

    for attempt in range(3):
        try:
            response = await _client.post(API_URL, headers=headers, json=payload)
            if response.is_error:
                try:
                    detail = response.json().get("error", {}).get("message", response.text)
                except Exception:
                    detail = response.text
                raise OpenRouterAPIError(response.status_code, str(detail)[:500])

            data = response.json()
            answer = data["choices"][0]["message"]["content"]
            if isinstance(answer, list):
                answer = "".join(
                    part.get("text", "") for part in answer
                    if isinstance(part, dict)
                )
            answer = str(answer or "").strip()
            if not answer:
                raise RuntimeError("OpenRouter returned an empty response")
            return answer

        except OpenRouterAPIError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                log.exception("OpenRouter request failed; code=%s model=%s", exc.code, MODEL)
                raise
            delay = min(2 ** (attempt + 1), 8) + random.uniform(0, 0.5)
            log.warning("Temporary OpenRouter error %s; retry %s/2 in %.1fs", exc.code, attempt + 1, delay)
            await asyncio.sleep(delay)

    raise RuntimeError("OpenRouter request failed")
