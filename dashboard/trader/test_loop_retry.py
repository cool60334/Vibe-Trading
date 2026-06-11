"""Tests for loop.call_with_retry — transient Bybit rate-limit backoff.

retCode 10006 ("Too many visits. Exceeded the API Rate Limit") surfaces from
ccxt as ``RateLimitExceeded`` (a ``NetworkError`` subclass). A single live loop
should not drop a trading bar — nor raise a scary ``Loop error`` alert — over a
momentary per-IP spike caused by another ccxt client on the same machine. It
should back off briefly and retry, only giving up after several attempts.
"""

import ccxt
import pytest

from trader.loop import call_with_retry


def _rate_limit() -> ccxt.RateLimitExceeded:
    return ccxt.RateLimitExceeded(
        'bybit {"retCode":10006,"retMsg":"Too many visits. '
        'Exceeded the API Rate Limit."}'
    )


def test_returns_immediately_on_success():
    slept: list[float] = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    result = call_with_retry(fn, sleeper=slept.append)

    assert result == "ok"
    assert calls["n"] == 1
    assert slept == []  # success → no backoff


def test_recovers_after_transient_rate_limit():
    slept: list[float] = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _rate_limit()
        return "recovered"

    result = call_with_retry(fn, max_attempts=4, base_sleep=5.0, sleeper=slept.append)

    assert result == "recovered"
    assert calls["n"] == 3          # failed twice, succeeded on third
    assert slept == [5.0, 10.0]     # exponential backoff between retries


def test_exhausts_and_reraises_after_max_attempts():
    slept: list[float] = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _rate_limit()

    with pytest.raises(ccxt.RateLimitExceeded):
        call_with_retry(fn, max_attempts=4, base_sleep=5.0, sleeper=slept.append)

    assert calls["n"] == 4          # tried max_attempts times
    assert slept == [5.0, 10.0, 20.0]  # backed off between each, not after last


def test_does_not_retry_non_network_error():
    slept: list[float] = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("logic bug, not transient")

    with pytest.raises(ValueError):
        call_with_retry(fn, sleeper=slept.append)

    assert calls["n"] == 1          # raised immediately
    assert slept == []              # no backoff for non-transient errors


def test_retries_generic_network_error_too():
    """Timeouts / ExchangeNotAvailable are also transient NetworkError subclasses."""
    slept: list[float] = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ccxt.RequestTimeout("bybit timeout")
        return "ok"

    result = call_with_retry(fn, base_sleep=1.0, sleeper=slept.append)

    assert result == "ok"
    assert calls["n"] == 2
    assert slept == [1.0]
