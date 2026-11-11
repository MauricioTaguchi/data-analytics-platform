import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import sleep

import pytest
from pydantic import ValidationError
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.requests import Request
from starlette.responses import Response

from app.core import cache
from app.core.cache import CacheService
from app.core.config import Settings, settings
from app.main import security_and_rate_limit


def test_authentication_cache_wait_does_not_block_the_event_loop(monkeypatch):
    started, release = Event(), Event()

    def delayed_counter(*_args):
        started.set()
        release.wait(timeout=2)
        return 1

    monkeypatch.setattr(CacheService, "increment", delayed_counter)

    async def exercise():
        request = Request({
            "type": "http", "method": "POST", "path": "/api/v1/auth/login",
            "headers": [], "client": ("127.0.0.1", 1234), "scheme": "http",
            "server": ("localhost", 80), "query_string": b"",
        })

        async def next_handler(_request):
            return Response(status_code=204)

        task = asyncio.create_task(security_and_rate_limit(request, next_handler))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            # The event loop can continue while the cache worker remains blocked.
            # A synchronous cache call would finish its wait before this resumes.
            assert not task.done()
        finally:
            release.set()
            response = await task
        assert response.status_code == 204

    asyncio.run(exercise())


def test_cache_deadlines_override_url_tuning_and_mutations_are_not_retried(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", "redis://localhost/0?socket_timeout=0&socket_connect_timeout=0&retry_on_timeout=true")
    monkeypatch.setattr(settings, "CACHE_REDIS_CONNECT_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(settings, "CACHE_REDIS_SOCKET_TIMEOUT_SECONDS", 0.5)
    client = cache._create_redis_client()
    connection = client.connection_pool.connection_class(**client.connection_pool.connection_kwargs)
    assert connection.socket_connect_timeout == 0.25
    assert connection.socket_timeout == 0.5
    calls = []

    def ambiguous_mutation():
        calls.append("increment")
        raise RedisTimeoutError("The response was lost after the command was sent.")

    with pytest.raises(RedisTimeoutError):
        connection.retry.call_with_retry(ambiguous_mutation, lambda _error: None)
    assert calls == ["increment"]


@pytest.mark.parametrize("name", ["CACHE_REDIS_CONNECT_TIMEOUT_SECONDS", "CACHE_REDIS_SOCKET_TIMEOUT_SECONDS"])
@pytest.mark.parametrize("value", [0, 10.1, float("inf"), float("nan")])
def test_cache_deadlines_must_be_finite_and_bounded(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


def test_cache_timeout_keeps_production_authentication_closed(client, monkeypatch):
    def timed_out_pipeline():
        raise RedisTimeoutError("Unavailable")

    monkeypatch.setattr(cache.redis_client, "pipeline", timed_out_pipeline)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    response = client.post("/api/v1/auth/login", json={
        "email": "missing@example.com", "password": "password12345",
    })
    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication protection is temporarily unavailable."}


def test_threaded_fallback_increments_do_not_lose_attempts(monkeypatch):
    class SlowReadCounters(OrderedDict):
        def get(self, key, default=None):
            value = super().get(key, default)
            sleep(0.001)
            return value

    def unavailable_pipeline():
        raise RedisTimeoutError("Unavailable")

    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(cache, "_fallback_counters", SlowReadCounters())
    monkeypatch.setattr(cache.redis_client, "pipeline", unavailable_pipeline)
    with ThreadPoolExecutor(max_workers=16) as pool:
        counts = list(pool.map(lambda _index: CacheService.increment("concurrent-counter", 60), range(128)))
    assert sorted(counts) == list(range(1, 129))
