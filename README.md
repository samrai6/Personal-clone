# Personal Clone Telegram Bot

A Kannada/English Telegram bot with TMDB movie and web-series lookup, title suggestions, PM relay, and optional Gemini AI chat.

## Features

- PM: Search a Movie or Web Series by exact title and show TMDB details.
- Groups: checks title-like messages against TMDB and suggests likely spelling matches.
- Existing reactions, source-channel relay, and owner tools remain available.
- Optional Gemini AI chat is controlled from the bot's private chat using `/ai on` and `/ai off`.
- AI is OFF by default. The ON/OFF setting is saved in the existing MongoDB settings and survives restarts.
- Normal movie searches continue to use TMDB; AI replies are requested explicitly with `/ai your question`.

## Setup

1. Create a Telegram bot with @BotFather.
2. Create a TMDB Read Access Token at https://www.themoviedb.org/settings/api.
3. Create a Gemini API key in Google AI Studio.
4. Set these private environment variables in Render:
   - `BOT_TOKEN`
   - `TMDB_BEARER_TOKEN`
   - `MONGODB_URI`
   - `GEMINI_API_KEY` (needed only if you want to enable Gemini)
5. Build command: `pip install -r requirements.txt`
6. Start command: `python bot.py`

## Gemini commands (private chat)

- `/ai on` — Owner only; turns AI on.
- `/ai off` — Owner only; turns AI off.
- `/ai status` — shows current state.
- `/ai your question` — asks Gemini when AI is ON.

The AI switch is not controlled by a Render environment variable. Only the API key is kept as a private environment secret. Free-tier quotas and model availability are controlled by Google and may change.

Keep all API keys out of this repository.
