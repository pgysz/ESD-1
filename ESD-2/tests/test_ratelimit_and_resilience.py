"""Unit tests for the rate limiter, circuit breaker, retry policy and client."""
import random

import pytest
import requests

from attendance_core.ratelimit import SlidingWindowRateLimiter, parse_limit
from clients.resilience import (CallFailedError, CircuitBreaker, CircuitOpenError,
                                ResilientClient, RetryPolicy)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- rate limiter

def test_parse_limit():
    assert parse_limit("10/60") == (10, 60.0)


def test_rate_limiter_blocks_then_allows_again_when_oldest_hit_expires():
    clock = Clock()
    rl = SlidingWindowRateLimiter(3, 10, clock=clock)
    assert [rl.check("ip")[0] for _ in range(3)] == [True, True, True]
    allowed, remaining, retry_after = rl.check("ip")
    assert (allowed, remaining) == (False, 0)
    assert retry_after == pytest.approx(10)
    clock.t = 4
    assert rl.check("ip")[2] == pytest.approx(6)          # oldest hit leaves at t=10
    clock.t = 10
    assert rl.check("ip")[0] is True                      # allowed again exactly then


def test_rate_limiter_is_per_client():
    rl = SlidingWindowRateLimiter(1, 60, clock=Clock())
    assert rl.check("a")[0] and rl.check("b")[0]
    assert not rl.check("a")[0]


# ------------------------------------------------------------- circuit breaker

def test_breaker_full_cycle():
    clock, changes = Clock(), []
    b = CircuitBreaker(3, 5.0, clock=clock, on_change=lambda o, n: changes.append((o, n)))
    for _ in range(3):
        b.before_call()
        b.record_failure()
    assert b.state == "OPEN"
    with pytest.raises(CircuitOpenError):
        b.before_call()                                    # blocked: no probe yet
    clock.t = 5.0
    b.before_call()                                        # recovery over: one probe
    assert b.state == "HALF_OPEN"
    with pytest.raises(CircuitOpenError):
        b.before_call()                                    # only ONE probe at a time
    b.record_success()
    assert b.state == "CLOSED"
    assert changes == [("CLOSED", "OPEN"), ("OPEN", "HALF_OPEN"), ("HALF_OPEN", "CLOSED")]


def test_breaker_failed_probe_reopens():
    clock = Clock()
    b = CircuitBreaker(1, 5.0, clock=clock)
    b.before_call(); b.record_failure()
    clock.t = 6; b.before_call()
    b.record_failure()
    assert b.state == "OPEN"
    clock.t = 8
    with pytest.raises(CircuitOpenError):
        b.before_call()                                    # waits a full new recovery period


def test_success_resets_the_failure_count():
    b = CircuitBreaker(3, 5.0, clock=Clock())
    for _ in range(2):
        b.before_call(); b.record_failure()
    b.before_call(); b.record_success()
    for _ in range(2):
        b.before_call(); b.record_failure()
    assert b.state == "CLOSED"                             # never 3 IN A ROW


# ---------------------------------------------------------------------- retry

def test_backoff_is_exponential_capped_and_jittered():
    policy = RetryPolicy(base_delay=0.2, max_delay=2.0, rng=random.Random(1))
    for n, ceiling in [(1, 0.2), (2, 0.4), (3, 0.8), (4, 1.6), (5, 2.0), (9, 2.0)]:
        assert all(0 <= policy.delay_before_retry(n) <= ceiling for _ in range(50))


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code, self.headers = status, headers or {}

    def json(self):
        return {}


class FakeSession:
    """Plays back scripted outcomes; counts calls."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def request(self, *args, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client_for(session, **kw):
    sleeps = []
    c = ResilientClient("http://x", session=session, sleep=sleeps.append, log=lambda _m: None,
                        breaker=kw.pop("breaker", CircuitBreaker(3, 5.0, clock=Clock())),
                        retry=RetryPolicy(rng=random.Random(0)), **kw)
    return c, sleeps


def test_retries_transient_failure_then_succeeds():
    s = FakeSession(FakeResponse(503), FakeResponse(503), FakeResponse(201))
    c, sleeps = client_for(s)
    assert c.request("POST", "/p", headers={"Idempotency-Key": "k"}).status_code == 201
    assert s.calls == 3 and len(sleeps) == 2


def test_retries_timeouts():
    s = FakeSession(requests.ReadTimeout(), FakeResponse(200))
    c, _ = client_for(s)
    assert c.request("GET", "/p").status_code == 200 and s.calls == 2


def test_gives_up_after_max_attempts():
    s = FakeSession(*[FakeResponse(503)] * 3)
    c, _ = client_for(s)
    with pytest.raises(CallFailedError):
        c.request("GET", "/p")
    assert s.calls == 3


def test_post_without_idempotency_key_is_not_retried():
    s = FakeSession(FakeResponse(503), FakeResponse(201))
    c, _ = client_for(s)
    with pytest.raises(CallFailedError, match="not safe to repeat"):
        c.request("POST", "/p")
    assert s.calls == 1


def test_client_errors_are_not_retried_and_do_not_trip_breaker():
    breaker = CircuitBreaker(1, 5.0, clock=Clock())
    s = FakeSession(FakeResponse(404))
    c, _ = client_for(s, breaker=breaker)
    assert c.request("GET", "/p").status_code == 404
    assert s.calls == 1 and breaker.state == "CLOSED"


def test_429_honours_retry_after_and_is_not_a_breaker_failure():
    breaker = CircuitBreaker(1, 5.0, clock=Clock())
    s = FakeSession(FakeResponse(429, {"Retry-After": "1"}), FakeResponse(200))
    c, sleeps = client_for(s, breaker=breaker)
    assert c.request("GET", "/p").status_code == 200
    assert sleeps == [1.0] and breaker.state == "CLOSED"


def test_open_breaker_blocks_without_touching_the_network():
    clock = Clock()
    breaker = CircuitBreaker(1, 5.0, clock=clock)
    breaker.before_call(); breaker.record_failure()
    s = FakeSession()                                      # any call would pop from empty -> error
    c, _ = client_for(s, breaker=breaker)
    with pytest.raises(CircuitOpenError):
        c.request("GET", "/p")
    assert s.calls == 0
