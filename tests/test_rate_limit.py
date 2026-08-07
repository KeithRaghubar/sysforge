"""
test_rate_limit.py — unit tests for sysforge.primitives.rate_limit.

Covers:
    parse_retry_after              — delta-seconds, HTTP-date, junk input
    RateLimiter                    — wait_before_rpc / wait_before_fetch / apply_retry_after
    http_get_with_rate_limit       — happy path + 429 + 503
    run_throttled_git              — success, rate-limit detection, non-rate-limit failure
"""
import email.utils
import subprocess
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from unittest.mock import MagicMock, patch

import pytest

from sysforge.primitives.rate_limit import (
    RATE_LIMIT_GIT_ERRORS,
    RateLimited,
    RateLimiter,
    http_get_with_rate_limit,
    parse_retry_after,
    run_throttled_git,
)


# ---------------------------------------------------------------------------
# parse_retry_after
# ---------------------------------------------------------------------------

def test_parse_retry_after_delta_seconds():
    assert parse_retry_after("42") == 42.0


def test_parse_retry_after_negative_clamped_to_zero():
    assert parse_retry_after("-5") == 0.0


def test_parse_retry_after_none_returns_none():
    assert parse_retry_after(None) is None


def test_parse_retry_after_empty_returns_none():
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None


def test_parse_retry_after_bad_input_returns_none():
    assert parse_retry_after("not-a-date") is None


def test_parse_retry_after_http_date_future():
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    header = email.utils.format_datetime(future)
    val = parse_retry_after(header)
    # Parsing has some wall-clock slack; the returned delay should be close to 30s.
    assert val is not None
    assert 10 < val <= 60


def test_parse_retry_after_http_date_past_is_zero():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    header = email.utils.format_datetime(past)
    assert parse_retry_after(header) == 0.0


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

def _fake_clock():
    """Return (sleep_fn, now_fn, ticks) — the sleep fn advances the fake clock.

    Starts at a large value so the initial last_git_fetch=0.0 doesn't look
    like a recent fetch (matches real time.monotonic() which is never zero).
    """
    ticks = {"t": 1000.0, "slept": []}

    def _sleep(s):
        ticks["slept"].append(s)
        ticks["t"] += s

    def _now():
        return ticks["t"]

    return _sleep, _now, ticks


def test_rate_limiter_no_wait_when_unthrottled():
    sleep_fn, now_fn, ticks = _fake_clock()
    rl = RateLimiter(_sleep=sleep_fn, _now=now_fn)
    assert rl.wait_before_rpc() == 0.0
    assert ticks["slept"] == []


def test_rate_limiter_apply_retry_after_then_wait_rpc():
    sleep_fn, now_fn, ticks = _fake_clock()
    rl = RateLimiter(_sleep=sleep_fn, _now=now_fn)
    rl.apply_retry_after(10.0, source="HTTP 429")
    waited = rl.wait_before_rpc()
    assert waited == 10.0
    assert ticks["slept"] == [10.0]
    # After waiting, penalty window has elapsed.
    assert rl.remaining_penalty_s() == 0.0


def test_rate_limiter_apply_retry_after_extends_not_shortens():
    sleep_fn, now_fn, _ = _fake_clock()
    rl = RateLimiter(_sleep=sleep_fn, _now=now_fn)
    rl.apply_retry_after(60.0)
    # A smaller second hit must not shrink the window.
    rl.apply_retry_after(5.0)
    assert rl.remaining_penalty_s() == 60.0


def test_rate_limiter_apply_retry_after_none_uses_default():
    sleep_fn, now_fn, _ = _fake_clock()
    rl = RateLimiter(default_retry_after_s=90.0, _sleep=sleep_fn, _now=now_fn)
    applied = rl.apply_retry_after(None)
    assert applied == 90.0
    assert rl.remaining_penalty_s() == 90.0


def test_rate_limiter_fetch_enforces_min_interval():
    sleep_fn, now_fn, ticks = _fake_clock()
    rl = RateLimiter(min_git_interval_s=0.5, _sleep=sleep_fn, _now=now_fn)
    # First fetch: no floor.
    rl.wait_before_fetch()
    assert ticks["slept"] == []
    # Second fetch immediately after: must wait full 0.5s.
    rl.wait_before_fetch()
    assert ticks["slept"] == [0.5]


def test_rate_limiter_fetch_stacks_penalty_and_min_interval():
    sleep_fn, now_fn, _ticks = _fake_clock()
    rl = RateLimiter(min_git_interval_s=0.5, _sleep=sleep_fn, _now=now_fn)
    rl.wait_before_fetch()
    rl.apply_retry_after(3.0, source="git 429")
    waited = rl.wait_before_fetch()
    # Penalty consumes 3.0 but by then enough time has passed to skip the floor.
    assert waited == 3.0


# ---------------------------------------------------------------------------
# http_get_with_rate_limit
# ---------------------------------------------------------------------------

def _http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://aur.archlinux.org/rpc", code=code,
        msg="nope", hdrs=headers, fp=None,
    )


def test_http_get_returns_body():
    sleep_fn, now_fn, _ticks = _fake_clock()
    rl = RateLimiter(_sleep=sleep_fn, _now=now_fn)
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        out = http_get_with_rate_limit("https://example/", rl)
    assert out == b"ok"


def test_http_get_429_raises_rate_limited_and_updates_window():
    sleep_fn, now_fn, _ = _fake_clock()
    rl = RateLimiter(_sleep=sleep_fn, _now=now_fn)
    err = _http_error(429, retry_after="30")
    with patch("urllib.request.urlopen", side_effect=err), pytest.raises(RateLimited):
        http_get_with_rate_limit("https://example/", rl)
    assert rl.remaining_penalty_s() == 30.0


def test_http_get_503_raises_rate_limited():
    sleep_fn, now_fn, _ = _fake_clock()
    rl = RateLimiter(_sleep=sleep_fn, _now=now_fn)
    err = _http_error(503, retry_after="15")
    with patch("urllib.request.urlopen", side_effect=err), pytest.raises(RateLimited):
        http_get_with_rate_limit("https://example/", rl)
    assert rl.remaining_penalty_s() == 15.0


def test_http_get_other_http_error_propagates():
    sleep_fn, now_fn, _ = _fake_clock()
    rl = RateLimiter(_sleep=sleep_fn, _now=now_fn)
    err = _http_error(500)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(urllib.error.HTTPError):
            http_get_with_rate_limit("https://example/", rl)
    # A 500 is not a throttle — no penalty.
    assert rl.remaining_penalty_s() == 0.0


# ---------------------------------------------------------------------------
# run_throttled_git
# ---------------------------------------------------------------------------

def _completed(returncode, stderr=""):
    return subprocess.CompletedProcess(
        ["git"], returncode, stdout="", stderr=stderr,
    )


def test_run_throttled_git_success_no_penalty():
    sleep_fn, now_fn, _ = _fake_clock()
    rl = RateLimiter(_sleep=sleep_fn, _now=now_fn)
    with patch("subprocess.run", return_value=_completed(0)):
        result = run_throttled_git(["git", "fetch"], rl)
    assert result.returncode == 0
    assert rl.remaining_penalty_s() == 0.0


@pytest.mark.parametrize("marker", RATE_LIMIT_GIT_ERRORS)
def test_run_throttled_git_detects_rate_limit(marker):
    sleep_fn, now_fn, _ = _fake_clock()
    rl = RateLimiter(_sleep=sleep_fn, _now=now_fn)
    with patch("subprocess.run",
               return_value=_completed(128, stderr=f"fatal: {marker}")):
        result = run_throttled_git(["git", "fetch"], rl)
    assert result.returncode == 128
    # Rate limit hit → default penalty applied.
    assert rl.remaining_penalty_s() > 0


def test_run_throttled_git_non_rate_limit_failure_no_penalty():
    sleep_fn, now_fn, _ = _fake_clock()
    rl = RateLimiter(_sleep=sleep_fn, _now=now_fn)
    with patch("subprocess.run",
               return_value=_completed(128, stderr="fatal: not a git repository")):
        run_throttled_git(["git", "fetch"], rl)
    assert rl.remaining_penalty_s() == 0.0
