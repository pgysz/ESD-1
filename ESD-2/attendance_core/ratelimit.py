"""Single-instance sliding-window-log rate limiter.

Keeps the timestamps of each caller's recent requests. A request is allowed if
fewer than `limit` timestamps fall inside the last `window` seconds. When it is
blocked, the caller can retry as soon as the OLDEST timestamp leaves the
window -- that exact moment is what we return in Retry-After.

Limits of this design (the lecture's section 10.3): state is in this
process's memory, so with several instances each would enforce its own limit;
a real deployment would keep the counters in a shared store such as Redis.
"""

import threading
import time
from collections import deque


def parse_limit(text):
    """'10/60' -> (10, 60.0)"""
    limit, window = text.split("/")
    return int(limit), float(window)


class SlidingWindowRateLimiter:
    def __init__(self, limit, window_seconds, clock=time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits = {}                      # key -> deque of timestamps
        self._lock = threading.Lock()

    def check(self, key):
        """Record an attempt. Returns (allowed, remaining, reset_seconds).

        reset_seconds = seconds until the oldest recorded hit leaves the window
        (0 when there are no hits). When allowed is False it is the Retry-After.
        """
        with self._lock:
            now = self._clock()
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, 0, max(0.0, self.window - (now - hits[0]))
            hits.append(now)
            remaining = self.limit - len(hits)
            return True, remaining, max(0.0, self.window - (now - hits[0]))
