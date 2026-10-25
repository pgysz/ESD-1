#!/usr/bin/env python3
"""Python caller for the HTTP API: acts like another service calling us.

    python clients/http_caller.py --demo

Needs the HTTP server started with:
    ENABLE_FAULT_INJECTION=1 SCAN_FAILURE_RATE=0 python app.py
(fault injection is how we trigger failures on demand; SCAN_FAILURE_RATE=0
stops random scan mismatches from muddying the output.)

The demo runs four phases in ONE run and prints a timeline:
  1. healthy        several successful calls
  2. retry          a call that fails transiently, is retried, then succeeds
  3. breaker opens  an outage: attempts fail, the breaker OPENS, next calls are
                    BLOCKED without touching the network
  4. recovery       after the recovery period one probe succeeds -> CLOSED again
"""

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clients.resilience import (CallFailedError, CircuitBreaker, CircuitOpenError,   # noqa: E402
                                ResilientClient, RetryPolicy)

START = time.monotonic()


def say(message):
    print(f"[{time.monotonic() - START:6.2f}s] {message}", flush=True)


def trace_headers(trace_id, extra=None):
    headers = {"traceparent": f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01",
               "X-Request-ID": uuid.uuid4().hex[:16]}
    headers.update(extra or {})
    return headers


def check_in(client, user_id, trace_id, fault=None, key=None):
    headers = trace_headers(trace_id, {"Idempotency-Key": key or str(uuid.uuid4())})
    if fault:
        headers["X-Inject-Fault"] = fault
    resp = client.request("POST", "/api/v1/attendance", headers=headers,
                          json={"user_id": user_id, "method": "face"})
    body = resp.json()
    say(f"  => {resp.status_code} for {user_id}: "
        f"{body.get('state') or body.get('title')} "
        f"(session {str(body.get('id', '-'))[:8]}, replayed={resp.headers.get('Idempotent-Replayed', 'false')})")
    return resp


def close_session(client, session_id):
    client.request("PATCH", f"/api/v1/attendance/{session_id}",
                   json={"state": "closed", "method": "face"})


def cleanup(client, user_ids):
    """Leave the shared database clean so the demo can be re-run."""
    for uid in user_ids:
        resp = client.request("GET", "/api/v1/attendance", params={"state": "open", "user_id": uid})
        for item in resp.json()["items"]:
            close_session(client, item["id"])


def demo(base_url):
    users = ["u16", "u17", "u18", "u19", "u20"]
    trace_id = uuid.uuid4().hex                   # one trace for the whole run

    def on_change(old, new):
        say(f"  ** circuit breaker {old} -> {new}")

    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=5.0, on_change=on_change)
    client = ResilientClient(base_url, timeout=(0.5, 1.0),
                             retry=RetryPolicy(max_attempts=3, base_delay=0.2, max_delay=2.0),
                             breaker=breaker, log=say)
    # Housekeeping calls use their own quiet client so they do not clutter the
    # timeline (and never share the demo client's circuit breaker).
    housekeeping = ResilientClient(base_url, log=lambda _msg: None)
    say("SETTINGS: timeout connect=0.5s read=1.0s | retries: 3 attempts total, backoff "
        "0.2s*2^n with jitter (cap 2s) | breaker: opens after 3 consecutive failures, "
        "recovery 5s | retries only for safe/idempotent requests")
    say(f"trace_id for this run: {trace_id}")
    cleanup(housekeeping, users)

    say("PHASE 1 - healthy: three successful check-ins")
    for uid in users[:3]:
        check_in(client, uid, trace_id)

    say("PHASE 2a - transient failure: server answers 503 twice, then recovers (same Idempotency-Key)")
    check_in(client, users[3], trace_id, fault="status=503,times=2", key=f"retry-demo-{uuid.uuid4().hex[:8]}")

    say("PHASE 2b - timeout: first attempt is slower than the 1.0s read timeout, retry succeeds")
    check_in(client, users[4], trace_id, fault="delay=1500,times=1", key=f"timeout-demo-{uuid.uuid4().hex[:8]}")
    time.sleep(1.0)       # let the abandoned slow request finish on the server (replays the stored result)
    cleanup(housekeeping, users)

    say("PHASE 3 - outage: every attempt fails with 503 -> breaker opens")
    try:
        check_in(client, users[0], trace_id, fault="status=503")
    except CallFailedError as exc:
        say(f"  => gave up: {exc}")
    say("PHASE 3b - breaker is OPEN: further calls are blocked WITHOUT contacting the server")
    for uid in users[1:3]:
        try:
            check_in(client, uid, trace_id)
        except CircuitOpenError as exc:
            say(f"  => BLOCKED for {uid}: {exc}")

    say("PHASE 4 - recovery: waiting 5s for the recovery period, then one probe call")
    time.sleep(5.1)
    check_in(client, users[0], trace_id)           # half-open probe succeeds -> CLOSED
    check_in(client, users[1], trace_id)           # normal again
    cleanup(housekeeping, users)
    say("DONE")


def main():
    parser = argparse.ArgumentParser(description="Resilient HTTP caller for the attendance API")
    parser.add_argument("--base-url", default="http://localhost:5000")
    parser.add_argument("--demo", action="store_true", help="run the four-phase demo")
    args = parser.parse_args()
    if not args.demo:
        parser.error("use --demo")
    demo(args.base_url)


if __name__ == "__main__":
    main()
