"""In-process rate limiter for login attempts (FR-AUTH-09 / T019).

Policy: at most LOGIN_MAX_ATTEMPTS failed attempts per LOGIN_WINDOW_SECONDS
per login key; further attempts get 429 TOO_MANY_ATTEMPTS.

Note: state is per-process memory, which is fine for the single-process
waitress deployment. If multiple workers/processes are ever used, this must
move to the database or a shared store.
"""
import time
from collections import defaultdict

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 10 * 60

# QR/numeric-code scan attempts (SRS §2.3: rate limiting on code entry too)
SCAN_MAX_ATTEMPTS = 6
SCAN_WINDOW_SECONDS = 60

_failures = defaultdict(list)


def is_blocked(login_key, max_attempts=LOGIN_MAX_ATTEMPTS,
               window_seconds=LOGIN_WINDOW_SECONDS, now=None):
    """True if the key exhausted its failed-attempt budget in the window."""
    if now is None:
        now = time.time()
    # Prune attempts outside the window as a side effect of checking.
    _failures[login_key] = [
        t for t in _failures[login_key] if now - t < window_seconds
    ]
    return len(_failures[login_key]) >= max_attempts


def record_failure(login_key):
    _failures[login_key].append(time.time())


def clear(login_key):
    """Forget failures (call after a successful login)."""
    _failures.pop(login_key, None)
