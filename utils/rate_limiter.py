"""
Simple in-process per-user rate limiter using a sliding window.

Security improvement: ColabLeechBot and Monster-Bot had no rate limiting at all,
making them trivially abusable.
"""

import time
from collections import defaultdict, deque
from config import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS


class RateLimiter:
    def __init__(self, max_requests: int = RATE_LIMIT_REQUESTS, window: int = RATE_LIMIT_WINDOW_SECONDS):
        self.max_requests = max_requests
        self.window = window
        self._hits: dict[int, deque] = defaultdict(deque)

    def is_allowed(self, user_id: int) -> bool:
        """Return True if the user is within their rate limit."""
        now = time.monotonic()
        q = self._hits[user_id]

        # Drop hits outside the window
        while q and now - q[0] > self.window:
            q.popleft()

        if len(q) >= self.max_requests:
            return False

        q.append(now)
        return True

    def next_available(self, user_id: int) -> float:
        """Return seconds until the user can make another request."""
        q = self._hits.get(user_id)
        if not q:
            return 0.0
        now = time.monotonic()
        oldest = q[0]
        wait = self.window - (now - oldest)
        return max(0.0, wait)


# Singleton used across the app
rate_limiter = RateLimiter()
