"""
G13: In-memory rate limiter — per-agent sliding window.
"""
import time
import threading
from collections import defaultdict
from functools import wraps
from flask import request, jsonify, g


class RateLimiter:
    """Simple in-memory sliding-window rate limiter."""

    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests = defaultdict(list)  # key -> list of timestamps
        self._lock = threading.Lock()

    def _cleanup(self, key: str, now: float):
        """Remove expired timestamps. M6 fix: also remove empty keys."""
        cutoff = now - self.window
        self._requests[key] = [t for t in self._requests[key] if t > cutoff]
        if not self._requests[key]:
            del self._requests[key]

    def is_allowed(self, key: str) -> tuple:
        """Check if request is allowed. Returns (allowed, remaining, reset_at)."""
        now = time.time()
        with self._lock:
            self._cleanup(key, now)
            current = len(self._requests[key])
            if current >= self.max_requests:
                reset_at = self._requests[key][0] + self.window
                return False, 0, reset_at
            self._requests[key].append(now)
            remaining = self.max_requests - current - 1
            return True, remaining, now + self.window


# Global rate limiter instances
_api_limiter = RateLimiter(max_requests=60, window_seconds=60)     # 60 req/min
_submit_limiter = RateLimiter(max_requests=10, window_seconds=60)  # 10 submissions/min


def rate_limit(limiter=None):
    """Decorator: rate limit based on authenticated agent or IP."""
    if limiter is None:
        limiter = _api_limiter

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            # Use agent ID if authenticated, else IP
            key = getattr(g, 'current_agent_id', None) or request.remote_addr or 'unknown'
            allowed, remaining, reset_at = limiter.is_allowed(key)

            if not allowed:
                resp = jsonify({
                    "error": "Rate limit exceeded",
                    "retry_after": max(0, int(reset_at - time.time())),
                })
                resp.status_code = 429
                resp.headers['Retry-After'] = str(max(1, int(reset_at - time.time())))
                resp.headers['X-RateLimit-Remaining'] = '0'
                return resp

            response = f(*args, **kwargs)
            # Add rate limit headers to successful responses
            if hasattr(response, 'headers'):
                response.headers['X-RateLimit-Remaining'] = str(remaining)
            return response
        return decorated
    return decorator


def get_submit_limiter():
    """Get the submission-specific rate limiter."""
    return _submit_limiter
