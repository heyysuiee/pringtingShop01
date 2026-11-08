from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """In-process sliding-window limiter. Suitable for the documented single-instance deployment."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, bucket: str, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events[(bucket, key)]
            while events and now - events[0] > window_seconds:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True

    def reset(self, bucket: str, key: str) -> None:
        """Forget the recorded events for one key (for example, give back a cooldown after a failed send)."""
        with self._lock:
            self._events.pop((bucket, key), None)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


limiter = SlidingWindowLimiter()

# Limits are deliberately conservative for a small print shop; adjust here if needed.
LOGIN_LIMIT = (10, 15 * 60)            # per client address and per submitted email
OTP_REQUEST_LIMIT = (5, 60 * 60)       # per client address and per submitted email
OTP_VERIFY_LIMIT = (20, 15 * 60)       # per client address
RESET_CONFIRM_LIMIT = (20, 15 * 60)    # per client address
OTP_RESEND_COOLDOWN_SECONDS = 60
