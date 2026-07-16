import json
from redis import Redis
from app.core.config import settings

redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)

class CacheService:
    DEFAULT_TTL_SECONDS = 900

    @classmethod
    def get_json(cls, key: str):
        value = redis_client.get(key)
        return json.loads(value) if value else None

    @classmethod
    def set_json(cls, key: str, value, ttl: int | None = None):
        redis_client.setex(
            key,
            ttl or cls.DEFAULT_TTL_SECONDS,
            json.dumps(value, ensure_ascii=False, default=str),
        )

    @classmethod
    def delete(cls, key: str):
        redis_client.delete(key)
