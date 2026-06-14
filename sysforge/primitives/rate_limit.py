# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
rate_limit.py — shared rate limiter for AUR RPC + git fetches.

Used by `source_sync.py` to pace both the batch AUR RPC call and per-package
shallow `git fetch` invocations. Sequential execution in the scheduler means
no burst management is needed; the model here is a simple minimum
inter-request delay plus a shared Retry-After penalty window.

Shared state (process-global):
  - ``not_before``: monotonic timestamp before which no request of any kind
    may go out. Set by ``apply_retry_after()`` when the server returns a
    Retry-After header or an equivalent git error. Wraps both the RPC and git
    paths so a 429 on one locks the other.

Public API:
    RateLimiter(min_git_interval_s=0.5, default_retry_after_s=60.0)
        .wait_before_fetch()         sleep until allowed, return seconds waited
        .wait_before_rpc()           no interval floor, just respect penalty
        .apply_retry_after(s)        set/extend the global not_before window
        .remaining_penalty_s()       seconds remaining in current lockout (0.0 if free)

    parse_retry_after(header)        -> float | None
    http_get_with_rate_limit(
        url, limiter, *, timeout=10,
    )                                -> bytes
        Issues a GET with the limiter gating both pre-call wait and any
        Retry-After response. Raises RateLimited if the response is 429/503.

    run_throttled_git(cmd, limiter, *, timeout=None)
        -> subprocess.CompletedProcess
        Wraps subprocess.run with the same semantics — waits for the limiter,
        invokes git, on RATE_LIMIT_GIT_ERRORS applies the default Retry-After.

Design notes:
  - Uses time.monotonic() so wall-clock jumps do not reopen the penalty.
  - All state is module-global and intentionally not thread-safe: the
    scheduler runs sequentially.
"""
import email.utils
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from sysforge import log
_rl_log = log.get_logger("RATELIMIT")


class RateLimited(Exception):
    """Raised when an HTTP request returned 429 or 503."""


def parse_retry_after(header: str | None) -> float | None:
    """Parse a Retry-After header value into seconds, or None if unparseable.

    Accepts either an integer seconds value (RFC 7231 §7.1.3 delta-seconds)
    or an HTTP-date. Returns None for empty/None input.
    """
    if not header:
        return None
    header = header.strip()
    if not header:
        return None
    try:
        return max(0.0, float(int(header)))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    now = time.time()
    return max(0.0, dt.timestamp() - now)


@dataclass
class RateLimiter:
    """Shared-state pacer across AUR RPC and git fetches.

    The ``not_before`` field is a monotonic timestamp; any request path
    (RPC or git) waits until the current monotonic clock passes it.
    """
    min_git_interval_s: float = 0.5
    default_retry_after_s: float = 60.0
    not_before: float = 0.0       # monotonic
    last_git_fetch: float = 0.0   # monotonic
    penalty_source: str = ""
    _sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    _now: Callable[[], float] = field(default=time.monotonic, repr=False)

    def remaining_penalty_s(self) -> float:
        return max(0.0, self.not_before - self._now())

    def _wait_for_penalty(self) -> float:
        delay = self.remaining_penalty_s()
        if delay > 0:
            self._sleep(delay)
        return delay

    def wait_before_rpc(self) -> float:
        """Sleep until the penalty window clears. No per-RPC interval floor."""
        return self._wait_for_penalty()

    def wait_before_fetch(self) -> float:
        """Sleep until both the penalty window clears and the fetch floor is met."""
        waited = self._wait_for_penalty()
        now = self._now()
        elapsed = now - self.last_git_fetch
        remaining = self.min_git_interval_s - elapsed
        if remaining > 0:
            self._sleep(remaining)
            waited += remaining
            now = self._now()
        self.last_git_fetch = now
        return waited

    def apply_retry_after(self, seconds: float | None, *, source: str = "") -> float:
        """Set/extend the global not_before window.

        ``seconds=None`` or ``seconds<=0`` applies the default (60s). The
        longer of the current window and the new penalty wins — a second 429
        arriving mid-lockout doesn't shorten the wait.
        """
        if seconds is None or seconds <= 0:
            seconds = self.default_retry_after_s
        target = self._now() + seconds
        if target > self.not_before:
            self.not_before = target
            self.penalty_source = source
            _rl_log.warn(
                f"rate-limit penalty: waiting {seconds:.0f}s before next request"
                + (f" (trigger: {source})" if source else "")
            )
        return seconds


def http_get_with_rate_limit(
    url: str, limiter: RateLimiter, *, timeout: int = 10,
) -> bytes:
    """GET url through the limiter; raise RateLimited on 429/503.

    Other network errors propagate as the original urllib exception.
    On 429/503, parses the Retry-After header (or falls back to the limiter
    default) and updates the shared penalty window before raising.
    """
    limiter.wait_before_rpc()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            retry = parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
            limiter.apply_retry_after(retry, source=f"HTTP {e.code}")
            raise RateLimited(f"HTTP {e.code} from {url}") from e
        raise


RATE_LIMIT_GIT_ERRORS = (
    "error: 429",
    "Too Many Requests",
    "error: 503",
    "error: 502",
)


def run_throttled_git(
    cmd: list, limiter: RateLimiter, *, timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Run a git subprocess through the limiter.

    On stderr matching a rate-limit marker, applies the default Retry-After
    window (git rarely surfaces the HTTP header). Returns the
    CompletedProcess on both success and non-rate-limit failure — callers
    decide what to do with transient errors.
    """
    limiter.wait_before_fetch()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        stderr = result.stderr or ""
        if any(marker in stderr for marker in RATE_LIMIT_GIT_ERRORS):
            limiter.apply_retry_after(None, source="git rate-limit")
    return result
