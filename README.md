# Personal Clone Telegram Bot

A lightweight Kannada/English Telegram movie lookup bot powered by TMDB. No Gemini or AI chat replies.

## Features

- PM: Search a Movie or Web Series by title and show TMDB details.
- IMDb Search button for every matched title.
- Search in @Samp_yu5bot button for movie-file lookup.
- Groups: checks short title-like messages against TMDB and replies with a spelling correction when a likely match is found.
- No AI replies and no AI daily quota.

## Setup

1. Create a Telegram bot with @BotFather.
2. Create a TMDB API key at https://www.themoviedb.org/settings/api.
3. Set these environment variables in Render (Background Worker):
   - BOT_TOKEN
   - TMDB_API_KEY
4. Build command: `pip install -r requirements.txt`
5. Start command: `python bot.py`

Keep API keys out of this public GitHub repository. The bot reads TMDB_API_KEY from the environment.
