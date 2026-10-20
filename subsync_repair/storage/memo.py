"""A small cache for results that hold as long as the files they came from do not change."""
import threading


class Memo:
    """Bounded and safe to share between threads; it starts over when it is full."""

    def __init__(self, limit):
        self.limit = limit
        self._items = {}
        self._lock = threading.Lock()

    def __len__(self):
        with self._lock:
            return len(self._items)

    def get(self, key, default=None):
        with self._lock:
            return self._items.get(key, default)

    def put(self, key, value):
        with self._lock:
            if len(self._items) >= self.limit:
                self._items.clear()
            self._items[key] = value
