# Personal Clone Telegram Bot

Kannada/English Telegram assistant with PM AI chat, movie information, movie-search deep links, group title spelling checks, and MongoDB daily quotas.

## Features

- PM: Gemini-powered Kannada/English replies.
- Movie title only: movie details plus a button opening the matching search in `@Samp_yu5bot`.
- OTT: official platform/date when found; otherwise reports Not announced. Estimates must be labelled unconfirmed.
- Groups: checks short title-like messages only and replies to the original message only when Google Search grounding verifies a different title with confidence >= 0.90. Ordinary group conversation is ignored.
- Quota: 50 AI PM requests per user per India calendar day (IST), stored in MongoDB. Admin IDs in `ADMIN_IDS` bypass the quota. Group spelling checks are not counted.

## Setup

1. Create a Telegram bot with `@BotFather` and copy its token.
2. Create a MongoDB Atlas cluster and copy its connection string.
3. Create a Gemini API key at https://aistudio.google.com/apikey.
4. Copy `.env.example` values into your hosting environment (or local `.env`).
5. Install and run:

```bash
pip install -r requirements.txt
python bot.py
```

## Render

Create a **Background Worker** connected to this repository.
- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Add all required environment variables from `.env.example`.

For Koyeb, use the same build/start commands and environment variables.

## Notes

- Gemini Free Tier has model-specific request/token limits and may change. The bot's 50/user/day quota is separate from Google's limits; high concurrent traffic can still hit provider rate limits.
- Google Search grounding availability and quota depend on the Gemini model, region, and Google account/project. Current movie/OTT facts cannot be guaranteed if grounding is unavailable.
- The movie link uses the `getfile-` start payload supported by the configured movie bot. The username and deep-link convention must remain valid.
- Never commit real API keys, bot tokens, or the `.env` file.
