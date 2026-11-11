import json
from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import cast

from redis import ConnectionPool, Redis
from redis.backoff import NoBackoff
from redis.connection import parse_url
from redis.retry import Retry

from app.core.config import settings

def _create_redis_client() -> Redis:
    # Explicit protection limits take precedence over URL tuning parameters.
    # Do not retry an INCR after an ambiguous network failure: it may have run.
    options = parse_url(settings.REDIS_URL)
    options.update(
        decode_responses=True,
        socket_connect_timeout=settings.CACHE_REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=settings.CACHE_REDIS_SOCKET_TIMEOUT_SECONDS,
        retry_on_timeout=False,
        retry_on_error=[],
        retry=Retry(NoBackoff(), 0),
    )
    return Redis(connection_pool=ConnectionPool(**options))


redis_client = _create_redis_client()
_fallback_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
_fallback_counters: OrderedDict[str, tuple[float, int]] = OrderedDict()
_fallback_lock = Lock()


def _fallback_allowed() -> bool:
    return settings.ENVIRONMENT.lower() in {"development", "test"}


def _require_fallback_allowed(exc: Exception) -> None:
    if not _fallback_allowed():
        raise RuntimeError("Redis is required in this environment.") from exc


def _trim(store: OrderedDict) -> None:
    while len(store) > settings.CACHE_FALLBACK_MAX_ITEMS:
        store.popitem(last=False)


class CacheService:
    DEFAULT_TTL_SECONDS = 900

    @classmethod
    def get_json(cls, key: str):
        try:
            value = cast(str | bytes | None, redis_client.get(key))
        except Exception as exc:
            _require_fallback_allowed(exc)
            with _fallback_lock:
                cached = _fallback_cache.get(key)
                if not cached:
                    return None
                expires_at, value = cached
                if expires_at <= monotonic():
                    _fallback_cache.pop(key, None)
                    return None
                _fallback_cache.move_to_end(key)
        return json.loads(value) if value else None

    @classmethod
    def set_json(cls, key: str, value, ttl: int | None = None):
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        expires_in = ttl if ttl is not None else cls.DEFAULT_TTL_SECONDS
        try:
            redis_client.setex(key, expires_in, encoded)
        except Exception as exc:
            _require_fallback_allowed(exc)
            fallback_ttl = min(expires_in, settings.CACHE_FALLBACK_TTL_SECONDS)
            with _fallback_lock:
                _fallback_cache[key] = (monotonic() + fallback_ttl, encoded)
                _fallback_cache.move_to_end(key)
                _trim(_fallback_cache)

    @classmethod
    def delete(cls, key: str):
        with _fallback_lock:
            _fallback_cache.pop(key, None)
            _fallback_counters.pop(key, None)
        try:
            redis_client.delete(key)
        except Exception as exc:
            _require_fallback_allowed(exc)

    @classmethod
    def increment(cls, key: str, window_seconds: int) -> int:
        try:
            pipeline = redis_client.pipeline()
            pipeline.incr(key)
            pipeline.expire(key, window_seconds, nx=True)
            count, _ = pipeline.execute()
            return int(count)
        except Exception as exc:
            _require_fallback_allowed(exc)
            with _fallback_lock:
                now = monotonic()
                expires_at, count = _fallback_counters.get(key, (now + window_seconds, 0))
                if expires_at <= now:
                    expires_at, count = now + window_seconds, 0
                count += 1
                _fallback_counters[key] = (expires_at, count)
                _fallback_counters.move_to_end(key)
                _trim(_fallback_counters)
                return count

    @staticmethod
    def ping() -> bool:
        try:
            return bool(redis_client.ping())
        except Exception as exc:
            _require_fallback_allowed(exc)
            return False
