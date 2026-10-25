"""Timeout + limited retry with backoff + circuit breaker for an HTTP caller.

This is the "other service calling our API" from Assignment 2 Part A. It is a
small library (so the unit tests can drive it with a fake clock) used by
clients/http_caller.py.

Settings (all configurable; the defaults are what the report states):
    timeout          connect 0.5 s, read 1.0 s
    retry limit      3 attempts in total (1 try + 2 retries)
    backoff          0.2 s * 2^(retry-1), full jitter, capped at 2 s
    breaker          opens after 3 consecutive failed attempts,
                     half-opens after a 5 s recovery period (one probe call)

What counts as a *transient* failure (worth retrying, and counted by the
breaker): connection error, timeout, HTTP 502/503/504.
What does NOT: other 4xx (the request is wrong -- retrying cannot fix it) and
429 (the server is healthy but limiting us -- we honour Retry-After instead and
do not count it against the breaker).
Only SAFE requests are retried: GET/HEAD/PUT/DELETE/OPTIONS, or a request that
carries an Idempotency-Key (the server then guarantees no duplicate effect).
"""

import random
import time

import requests

SAFE_METHODS = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
TRANSIENT_STATUSES = {502, 503, 504}


class CircuitOpenError(Exception):
    """Raised without touching the network while the breaker is open."""

    def __init__(self, retry_in):
        super().__init__(f"circuit breaker is OPEN (probe allowed in {retry_in:.1f}s)")
        self.retry_in = retry_in


class CallFailedError(Exception):
    """All attempts used up (or the request was not safe to retry)."""


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

    def __init__(self, failure_threshold=3, recovery_timeout=5.0, clock=time.monotonic,
                 on_change=None):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._on_change = on_change or (lambda old, new: None)
        self.state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    def _set(self, new):
        old, self.state = self.state, new
        if old != new:
            self._on_change(old, new)

    def before_call(self):
        """Call before every attempt; raises CircuitOpenError to block it."""
        if self.state == self.OPEN:
            waited = self._clock() - self._opened_at
            if waited < self.recovery_timeout:
                raise CircuitOpenError(self.recovery_timeout - waited)
            self._set(self.HALF_OPEN)             # recovery period over: allow ONE probe
            self._probe_in_flight = True
            return
        if self.state == self.HALF_OPEN:
            if self._probe_in_flight:
                raise CircuitOpenError(0.0)       # only one probe at a time
            self._probe_in_flight = True

    def record_success(self):
        self._failures = 0
        self._probe_in_flight = False
        self._set(self.CLOSED)

    def record_failure(self):
        self._probe_in_flight = False
        if self.state == self.HALF_OPEN:          # the probe failed: back to OPEN
            self._opened_at = self._clock()
            self._set(self.OPEN)
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self._clock()
            self._set(self.OPEN)


class RetryPolicy:
    def __init__(self, max_attempts=3, base_delay=0.2, max_delay=2.0, rng=None):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._rng = rng or random.Random()

    def delay_before_retry(self, retry_number):
        """Exponential backoff with full jitter: uniform(0, min(cap, base*2^(n-1)))."""
        ceiling = min(self.max_delay, self.base_delay * (2 ** (retry_number - 1)))
        return self._rng.uniform(0, ceiling)


class ResilientClient:
    def __init__(self, base_url, *, timeout=(0.5, 1.0), retry=None, breaker=None,
                 session=None, sleep=time.sleep, log=print, max_retry_after=2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry = retry or RetryPolicy()
        self.breaker = breaker or CircuitBreaker()
        self.session = session or requests.Session()
        self._sleep = sleep
        self._log = log
        self.max_retry_after = max_retry_after

    def request(self, method, path, *, headers=None, json=None, params=None):
        method = method.upper()
        headers = dict(headers or {})
        safe = method in SAFE_METHODS or "Idempotency-Key" in headers
        max_attempts = self.retry.max_attempts if safe else 1
        last = None
        for attempt in range(1, max_attempts + 1):
            self.breaker.before_call()            # may raise CircuitOpenError (no network)
            started = time.monotonic()
            try:
                resp = self.session.request(method, self.base_url + path, headers=headers,
                                            json=json, params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = f"{type(exc).__name__}"
                self.breaker.record_failure()
                self._log(f"  attempt {attempt}/{max_attempts}: {last} after "
                          f"{time.monotonic() - started:.2f}s -> failure "
                          f"(breaker {self.breaker.state})")
            else:
                if resp.status_code == 429:       # healthy but limiting us: not a failure
                    wait = float(resp.headers.get("Retry-After", "1"))
                    self._log(f"  attempt {attempt}/{max_attempts}: 429 Retry-After={wait:g}s "
                              f"(not counted by breaker)")
                    if safe and wait <= self.max_retry_after and attempt < max_attempts:
                        self._sleep(wait)
                        continue
                    return resp
                if resp.status_code in TRANSIENT_STATUSES:
                    last = f"HTTP {resp.status_code}"
                    self.breaker.record_failure()
                    self._log(f"  attempt {attempt}/{max_attempts}: HTTP {resp.status_code} "
                              f"-> transient failure (breaker {self.breaker.state})")
                else:
                    self.breaker.record_success()
                    self._log(f"  attempt {attempt}/{max_attempts}: HTTP {resp.status_code} "
                              f"in {time.monotonic() - started:.2f}s (breaker {self.breaker.state})")
                    return resp
            if attempt < max_attempts:
                delay = self.retry.delay_before_retry(attempt)
                self._log(f"  retrying in {delay:.2f}s (exponential backoff + jitter)")
                self._sleep(delay)
        why = "" if safe else " (not retried: request is not safe to repeat)"
        raise CallFailedError(f"{method} {path} failed after {max_attempts} attempt(s): {last}{why}")
