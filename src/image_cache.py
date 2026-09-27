"""Small in-process LRU for JPEG bytes on the Vercel cloud app."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict


class ImageByteCache:
    def __init__(self, *, max_items: int = 256, ttl_sec: float = 3600.0) -> None:
        self.max_items = max(8, max_items)
        self.ttl_sec = max(30.0, ttl_sec)
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, bytes]] = OrderedDict()

    def get(self, key: str) -> bytes | None:
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            stored_at, data = item
            if now - stored_at > self.ttl_sec:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return data

    def put(self, key: str, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._items[key] = (time.time(), data)
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)


# Warm instances keep recent check images so repeat grid/history loads are fast.
IMAGE_CACHE = ImageByteCache(max_items=320, ttl_sec=6 * 3600)
