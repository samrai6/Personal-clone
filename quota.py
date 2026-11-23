"""MongoDB-backed per-user daily message quota (Asia/Kolkata calendar day)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument

IST = ZoneInfo("Asia/Kolkata")
DAILY_LIMIT = 50


def india_date() -> str:
    return datetime.now(IST).date().isoformat()


class DailyQuota:
    def __init__(self, database, limit: int = DAILY_LIMIT):
        self.collection = database["daily_ai_usage"]
        self.limit = limit

    async def initialize(self) -> None:
        # One document per Telegram user; _id is already uniquely indexed by MongoDB.
        return None

    async def consume(self, user_id: int) -> tuple[bool, int]:
        """Consume one message. Returns (allowed, messages_used_today).

        The aggregation-pipeline update is atomic, so simultaneous messages from
        the same user cannot bypass the daily cap.
        """
        today = india_date()
        same_day = {"$eq": ["$date", today]}
        below_limit = {"$lt": [{"$ifNull": ["$count", 0]}, self.limit]}
        allowed_expr = {"$or": [{"$not": [same_day]}, below_limit]}

        doc = await self.collection.find_one_and_update(
            {"_id": int(user_id)},
            [
                {
                    "$set": {
                        "date": today,
                        "count": {
                            "$cond": [
                                {"$not": [same_day]},
                                1,
                                {
                                    "$cond": [
                                        below_limit,
                                        {"$add": [{"$ifNull": ["$count", 0]}, 1]},
                                        self.limit,
                                    ]
                                },
                            ]
                        },
                        "allowed": allowed_expr,
                    }
                }
            ],
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return bool(doc["allowed"]), int(doc["count"])
