"""Small process-local result cache; authorization always precedes lookup."""
from collections import OrderedDict
from dataclasses import dataclass
import json
from threading import Lock
from time import monotonic
from typing import Callable

from app.core.config import settings


@dataclass
class Entry:
    expires_at: float
    payload: bytes


class ChartCache:
    def __init__(self, clock: Callable[[], float] = monotonic):
        self._entries: OrderedDict[str, Entry] = OrderedDict()
        self._bytes = 0
        self._lock = Lock()
        self._clock = clock

    def get(self, key: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                self._bytes -= len(self._entries.pop(key).payload)
                return None
            self._entries.move_to_end(key)
            payload = entry.payload
        return json.loads(payload)

    def put(self, key: str, result: dict) -> None:
        payload = json.dumps(result, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(payload) > settings.DASHBOARD_CACHE_MAX_BYTES or settings.DASHBOARD_CACHE_TTL_SECONDS == 0:
            return
        now = self._clock()
        with self._lock:
            if old := self._entries.pop(key, None):
                self._bytes -= len(old.payload)
            for old_key, entry in list(self._entries.items()):
                if entry.expires_at <= now:
                    self._bytes -= len(self._entries.pop(old_key).payload)
            while self._entries and (
                len(self._entries) >= settings.DASHBOARD_CACHE_MAX_ENTRIES
                or self._bytes + len(payload) > settings.DASHBOARD_CACHE_MAX_BYTES
            ):
                self._bytes -= len(self._entries.popitem(last=False)[1].payload)
            self._entries[key] = Entry(now + settings.DASHBOARD_CACHE_TTL_SECONDS, payload)
            self._bytes += len(payload)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


chart_cache = ChartCache()
