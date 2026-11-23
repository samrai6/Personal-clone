# Personal Clone Telegram Bot

Starter configuration for the Personal Clone bot.

## Daily AI message quota

- Each Telegram user gets **50 AI messages per India calendar day**.
- The counter resets at **12:00 AM Asia/Kolkata**.
- The limit is per user, not shared across all users.
- Admin IDs configured in `ADMIN_IDS` are intended to bypass the quota.
- MongoDB stores usage so it persists across bot restarts and deployments.
- The quota module uses an atomic MongoDB update to prevent concurrent requests from exceeding the cap.

## Environment variables

Copy `.env.example` to `.env` and configure:

- `BOT_TOKEN`
- `MONGODB_URI`
- `MONGODB_NAME`
- `ADMIN_IDS` (optional, comma-separated Telegram user IDs)
- `GEMINI_API_KEY` and `GEMINI_MODEL` when Gemini integration is added.

## Quota module

Import `DailyQuota` from `quota.py`, initialize it with a MongoDB database, then call:

```python
allowed, used = await quota.consume(message.from_user.id)
if not allowed:
    await message.reply_text("ನಿಮ್ಮ ಇಂದಿನ 50 AI messages ಮುಗಿದಿವೆ. ನಾಳೆ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.")
    return
```

The bot handler should check whether the sender is an admin before calling `consume()`.
